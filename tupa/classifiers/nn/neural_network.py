from collections import OrderedDict
from typing import Dict

import numpy as np
import torch
import torch.nn as nn
from allennlp.data.vocabulary import Vocabulary
from allennlp.models import Model
from allennlp.modules.token_embedders import Embedding
from allennlp.nn.util import sequence_cross_entropy_with_logits
from tqdm import tqdm

from .birnn import EmptyRNN, BiRNN
from .constants import TRAINERS, TRAINER_LEARNING_RATE_PARAM_NAMES, TRAINER_KWARGS, CategoricalParameter
from .mlp import MultilayerPerceptron
from .sub_model import SubModel
from ..classifier import Classifier
from ...config import Config, BIRNN
from ...model_util import MISSING_VALUE, remove_existing

BIRNN_TYPES = {BIRNN: BiRNN}

tqdm.monitor_interval = 0


class AxisModel:
    """
    Framework-specific parameters that are part of the network
    """
    def __init__(self, axis, num_labels, config, model, birnn_type):
        args = config.hyperparams.specific[axis]
        self.birnn = birnn_type(config, args, model, save_path=("axes", axis, "birnn"))
        self.mlp = MultilayerPerceptron(config, args, model, num_labels=num_labels, save_path=("axes", axis, "mlp"))


class NeuralNetwork(Classifier, SubModel, Model):
    """
    Neural network to be used by the parser for action classification. Uses dense features.
    Keeps weights in constant-size matrices. Does not allow adding new features on-the-fly.
    Allows adding new labels on-the-fly, but requires pre-setting maximum number of labels.
    Expects features from DenseFeatureExtractor.
    """

    def __init__(self, *args, **kwargs):
        """
        Create a new untrained NN
        """
        Classifier.__init__(self, *args, **kwargs)
        SubModel.__init__(self)
        Model.__init__(self, vocab=Vocabulary())
        self.minibatch_size = self.config.args.minibatch_size
        self.loss = self.config.args.loss
        self.weight_decay = self.config.args.weight_decay
        self.axes = OrderedDict()  # string (axis) -> AxisModel
        self.losses = []
        self.steps = 0
        self.trainer_type = self.trainer = self.value = self.birnn = self.log_softmax = None

        if self.config.args.use_bert:
            import torch
            from pytorch_pretrained_bert import BertTokenizer, BertModel
            import logging

            self.torch = torch
            if self.config.args.bert_multilingual is not None:
                assert "multilingual" in self.config.args.bert_model
            logging.basicConfig(level=logging.INFO)
            is_uncased_model = "uncased" in self.config.args.bert_model
            self.tokenizer = BertTokenizer.from_pretrained(self.config.args.bert_model, do_lower_case=is_uncased_model)
            self.bert_model = BertModel.from_pretrained(self.config.args.bert_model)
            self.bert_model.eval()
            if self.config.args.dynet_gpu or self.config.args.pytorch_gpu:
                self.bert_model.to("cuda")
            self.bert_layers_count = 24 if "large" in self.config.args.bert_model else 12
            self.bert_embedding_len = 1024 if "large" in self.config.args.bert_model else 768

            self.last_weights = ""
        else:
            self.torch = self.tokenizer = self.bert_model = self.bert_layers_count = self.bert_embedding_len = \
                self.last_weights = None

    @property
    def input_dim(self):
        return OrderedDict((a, m.mlp.input_dim) for a, m in self.axes.items())
    
    @property
    def birnn_type(self):
        return BIRNN_TYPES.get(self.model_type, EmptyRNN)

    def resize(self):
        for axis, labels in self.labels.items():
            if labels.size is not None:
                num_labels = self.num_labels[axis]
                assert num_labels <= labels.size, "Exceeded maximum number of labels at axis '%s': %d > %d:\n%s" % (
                    axis, num_labels, labels.size, "\n".join(map(str, labels.all)))

    def init_model(self, axis=None, train=False):
        init = self.model is None
        if init:
            self.model = self
            self.birnn = self.birnn_type(self.config, Config().hyperparams.shared, self.model,
                                         save_path=("shared", "birnn"), shared=True)
            self.log_softmax = nn.LogSoftmax()
        if train:
            self.init_trainer()
        if axis:
            self.init_axis_model(axis)
        if init:
            self.finished_step()

    def init_trainer(self):
        if self.trainer_type is None or str(self.trainer_type) != self.config.args.optimizer:
            self.trainer_type = Trainer(self.config.args.optimizer)
            trainer_kwargs = dict(TRAINER_KWARGS.get(str(self.trainer_type), {}))
            learning_rate_param_name = TRAINER_LEARNING_RATE_PARAM_NAMES.get(str(self.trainer_type))
            if learning_rate_param_name and self.learning_rate:
                trainer_kwargs[learning_rate_param_name] = self.learning_rate
            self.config.print("Initializing trainer=%s(%s)" % (
                self.trainer_type, ", ".join("%s=%s" % (k, v) for k, v in trainer_kwargs.items())), level=4)
            self.trainer = self.trainer_type()(self.params.values(), **trainer_kwargs)

    def init_axis_model(self, axis, init=True):
        if axis in self.axes:
            if init:
                return
        else:
            self.axes[axis] = AxisModel(axis, self.labels[axis].size, self.config, self.model, self.birnn_type)
            self.config.print("Initializing %s model with %d labels" % (axis, self.labels[axis].size), level=4)
        indexed_dim = np.array([0, 0], dtype=int)  # specific, shared
        indexed_num = np.array([0, 0], dtype=int)
        for key, param in sorted(self.input_params.items()):
            if not param.enabled:
                continue
            if (not self.config.args.use_default_word_embeddings or self.config.args.bert_multilingual is not None) \
                    and key == 'W':
                i = self.birnn_indices(param)
                indexed_num[i] = np.fmax(indexed_num[i], param.num)  # indices to be looked up are collected
                continue
            if self.config.args.bert_multilingual is not None and param.lang_specific:
                continue
            self.config.print("Initializing input parameter: %s" % param, level=4)
            if not param.numeric and key not in self.params:  # lookup feature
                if init:
                    lookup = Embedding(num_embeddings=param.size, embedding_dim=param.dim, trainable=param.updated)
                    param.init_data()
                    if param.init is not None and param.init.size:
                        lookup.weight = nn.Parameter(param.init)
                    self.params[key] = lookup
            if param.indexed:
                i = self.birnn_indices(param)
                indexed_dim[i] += param.dim  # add to the input dimensionality at each indexed time point
                indexed_num[i] = np.fmax(indexed_num[i], param.num)  # indices to be looked up are collected

        if self.config.args.use_bert and init:
            if self.config.args.bert_layers_pooling == "weighed":
                self.params["bert_weights"] = self.model.add_parameters(len(self.config.args.bert_layers), init=1)
            indexed_dim[1] += self.bert_embedding_len
            if self.config.args.bert_multilingual == 0:
                indexed_dim[1] += 50

        for birnn in self.get_birnns(axis):
            birnn.init_params(indexed_dim[int(birnn.shared)], indexed_num[int(birnn.shared)])

    def birnn_indices(self, param):  # both specific and shared or just specific
        return [0, 1]

    def init_cg(self, renew=True):
        if renew:
            check_validity = self.config.args.dynet_check_validity
            dy.renew_cg(immediate_compute=check_validity, check_validity=check_validity)
        self.empty_values.clear()

    def get_empty_values(self, key):
        value = self.empty_values.get(key)
        if value is None:
            self.empty_values[key] = value = dy.inputVector(np.zeros(self.input_params[key].dim, dtype=float))
        return value

    def get_bert_embed(self, passage, lang, train=False):
        orig_tokens = passage
        bert_tokens = []
        # Token map will be an int -> int mapping between the `orig_tokens` index and
        # the `bert_tokens` index.
        orig_to_tok_map = []

        # Example:
        # orig_tokens = ["John", "Johanson", "'s",  "house"]
        # bert_tokens == ["[CLS]", "john", "johan", "##son", "'", "s", "house", "[SEP]"]
        # orig_to_tok_map == [(1), (2,3), (4,5), (6)]

        bert_tokens.append("[CLS]")
        for orig_token in orig_tokens:
            start_token = len(bert_tokens)
            bert_token = self.tokenizer.tokenize(orig_token)
            bert_tokens.extend(bert_token)
            end_token = start_token + len(bert_token)
            orig_to_tok_map.append(slice(start_token, end_token))
        bert_tokens.append("[SEP]")

        indexed_tokens = self.tokenizer.convert_tokens_to_ids(bert_tokens)
        tokens_tensor = self.torch.tensor([indexed_tokens])
        if self.config.args.dynet_gpu or self.config.args.pytorch_gpu:
            tokens_tensor = tokens_tensor.to("cuda")

        with self.torch.no_grad():
            encoded_layers, _ = self.bert_model(tokens_tensor)
        assert len(encoded_layers) == self.bert_layers_count, "Invalid BERT layer count %s" % len(encoded_layers)

        aligned_layer = []
        for layer in range(self.bert_layers_count):
            aligned_layer.append([])
            for mapping_range in orig_to_tok_map:
                token_embeddings = encoded_layers[layer][0][mapping_range]
                if self.config.args.bert_token_align_by == "mean":
                    aligned_layer[layer].append(self.torch.mean(token_embeddings, dim=(0,)).cpu().data.numpy())
                elif self.config.args.bert_token_align_by == "sum":
                    aligned_layer[layer].append(self.torch.sum(token_embeddings, dim=(0,)).cpu().data.numpy())
                elif self.config.args.bert_token_align_by == "first":
                    aligned_layer[layer].append(token_embeddings[0].cpu().data.numpy())
                else:
                    raise ValueError("Invalid BERT token align option '%s'" % self.config.args.bert_token_align_by)

        layer_list_to_use = self.config.args.bert_layers
        aligned_layer = [aligned_layer[i] for i in layer_list_to_use]

        if self.config.args.bert_layers_pooling == "weighed":
            bert_softmax = dy.softmax(self.params["bert_weights"])
            embeds = dy.cmult(dy.inputTensor(np.asarray(aligned_layer)), bert_softmax)
            embeds = dy.sum_dim(embeds, [0])
        elif self.config.args.bert_layers_pooling == "concat":
            embeds = dy.inputTensor(np.concatenate(aligned_layer, axis=1))
        elif self.config.args.bert_layers_pooling == "sum":
            embeds = dy.inputTensor(np.sum(aligned_layer, axis=0))
        else:
            raise ValueError("Invalid BERT pooling option '%s'" % self.config.args.bert_layers_pooling)

        if self.config.args.bert_multilingual == 0:
            assert lang
            if (lang + "_embed") in self.params:
                lang_embed = self.params[lang + "_embed"]
            else:
                lang_embed = self.model.add_parameters(50, init='glorot')
                self.params[lang + "_embed"] = lang_embed

            multilingual_embeds = []
            for embed in embeds:
                multilingual_embeds.append(dy.concatenate([lang_embed, embed]))

            embeds = dy.transpose(dy.concatenate_cols(multilingual_embeds))

        if self.config.args.bert_layers_pooling == "weighed":
            single_token_embed_len = self.bert_embedding_len
        elif self.config.args.bert_layers_pooling == "concat":
            single_token_embed_len = self.bert_embedding_len * len(layer_list_to_use)
        elif self.config.args.bert_layers_pooling == "sum":
            single_token_embed_len = self.bert_embedding_len
        else:
            raise ValueError("Invalid BERT pooling option '%s'" % self.config.args.bert_layers_pooling)
        if self.config.args.bert_multilingual == 0:
            single_token_embed_len += 50

        # TODO: try dropout strategies like dropping at the per layer embeddings or dropping entire layers.
        assert embeds.dim() == ((len(passage), single_token_embed_len), 1), "Invalid BERT dim %s" % embeds.dim()

        assert 0 <= self.config.args.bert_dropout < 1, "Invalid BERT dropout %s" % self.config.args.bert_dropout
        if train:
            embeds = dy.dropout(embeds, self.config.args.bert_dropout)

        return embeds

    def init_features(self, features, axes, train=False, passage=None, lang=None):
        for axis in axes:
            self.init_model(axis, train)
        embeddings = [[], []]  # specific, shared
        self.config.print("Initializing %s %s features for %d elements" %
                          (", ".join(axes), self.birnn_type.__name__, len(features)), level=4)
        for key, indices in sorted(features.items()):
            param = self.input_params[key]
            lookup = self.params.get(key)
            if not param.indexed or lookup is None:
                continue
            vectors = lookup.forward(torch.LongTensor(indices))
            for index in self.birnn_indices(param):
                embeddings[index].append((key, vectors))
            self.config.print(lambda: "%s: %s" % (key, ", ".join("%d->%s" % (i, e)
                                                                 for i, e in zip(indices, vectors))), level=4)
        if self.config.args.use_bert:
            bert_emded = self.get_bert_embed(passage, lang, train)
            embeddings[1].append(('BERT', bert_emded))

            if "bert_weights" in self.params:
                self.config.print(self.print_bert_weights)

        for birnn in self.get_birnns(*axes):
            birnn.init_features(embeddings[int(birnn.shared)], train)

    def print_bert_weights(self):
        bert_weights = str(self.params["bert_weights"].value())
        s = "\n--Bert Weights--: " + bert_weights
        if bert_weights != self.last_weights:
            s += "\n--Bert Weights Changed-- "
        self.last_weights = bert_weights
        return s

    def generate_inputs(self, features, axis):
        indices = []  # list, not set, in order to maintain consistent order
        for key, values in sorted(features.items()):
            param = self.input_params[key]
            lookup = self.params.get(key)
            if self.config.args.bert_multilingual is not None and param.lang_specific and key != 'W':
                continue
            if param.numeric:
                yield key, torch.DoubleTensor(values)
            elif param.indexed:  # collect indices to be looked up
                indices += values  # DenseFeatureExtractor collapsed features so there are no repetitions between them
            elif lookup is None:  # ignored
                continue
            else:  # lookup feature
                for x in lookup.forward(torch.LongTensor(values)):
                    yield (key, x)
            self.config.print(lambda: "%s: %s" % (key, values), level=4)
        if indices:
            for birnn in self.get_birnns(axis):
                yield from birnn.evaluate(indices)

    def get_birnns(self, *axes):
        """ Return shared + axis-specific BiRNNs """
        return [m.birnn for m in [self] + [self.axes[axis] for axis in axes]]

    def forward(self, *inputs) -> Dict[str, torch.Tensor]:
        pass

    def evaluate(self, features, axis, train=False):
        """
        Apply MLP and log softmax to input features
        :param features: dictionary of key, values for each feature type
        :param axis: axis of the label we are predicting
        :param train: whether to apply dropout
        :return: expression corresponding to log softmax applied to MLP output
        """
        self.init_model(axis, train)
        value = self.value.get(axis)
        if value is None:
            self.value[axis] = value = self.axes[axis].mlp.evaluate(self.generate_inputs(features, axis), train=train)
        return value

    def score(self, features, axis):
        """
        Calculate score for each label
        :param features: extracted feature values, of size input_size
        :param axis: axis of the label we are predicting
        :return: array with score for each label
        """
        super().score(features, axis)
        num_labels = self.num_labels[axis]
        if self.updates > 0 and num_labels > 1:
            return self.evaluate(features, axis).npvalue()[:num_labels]
        self.config.print("  no updates done yet, returning zero vector.", level=4)
        return np.zeros(num_labels)

    def update(self, features, axis, pred, true):
        """
        Update classifier weights according to predicted and true labels
        :param features: extracted feature values, in the form of a dict (name: value)
        :param axis: axis of the label we are predicting
        :param pred: label predicted by the classifier (non-negative integer bounded by num_labels[axis])
        :param true: true labels (non-negative integers bounded by num_labels[axis])
        """
        super().update(features, axis, pred, true, importance)
        losses = self.calc_loss(self.evaluate(features, axis, train=True), axis, true, importance or [1] * len(true))
        self.config.print(lambda: "  loss=" + ", ".join("%g" % l for l in losses), level=4)
        self.losses += losses
        self.steps += 1

    @staticmethod
    def calc_loss(scores, axis, true, importance):
        return sequence_cross_entropy_with_logits(scores, torch.LongTensor(true), torch.FloatTensor(importance))

    def finished_step(self, train=False):
        super().invalidate_caches()

    def invalidate_caches(self):
        self.value = {}  # For caching the result of _evaluate

    def finished_item(self, train=False):
        if self.steps >= self.minibatch_size:
            self.finalize()
        self.finished_step(train)

    def finalize(self, finished_epoch=False, **kwargs):
        """
        Fit this model on collected samples
        :return self
        """
        super().finalize(finished_epoch=finished_epoch, **kwargs)
        assert self.model, "Cannot finalize a model without initializing it first"
        # Drop unused axes (MLP params are initialized on first evaluate)
        self.axes = OrderedDict((a, m) for a, m in self.axes.items() if m.mlp.params)
        self.labels = OrderedDict((a, l) for a, l in self.labels.items() if a in self.axes)
        if self.losses:
            loss = sum(self.losses)
            loss.forward()
            self.config.print(lambda: "Total loss from %d time steps: %g" % (self.steps, loss.value()), level=4)
            loss.backward()
            self.trainer.step()
            self.losses = []
            self.steps = 0
            self.updates += 1
        return self
            
    def sub_models(self):
        """ :return: ordered list of SubModels """
        axes = list(filter(None, map(self.axes.get, self.labels or self.labels_t)))
        return [self] + [m.mlp for m in axes] + [m.birnn for m in axes + [self]]
    
    def save_sub_model(self, d, *args):
        return SubModel.save_sub_model(
            self, d,
            ("loss", self.loss),
            ("weight_decay", self.weight_decay),
        )

    def load_sub_model(self, d, *args, **kwargs):
        d = SubModel.load_sub_model(self, d, *args, **kwargs)
        self.config.args.loss = self.loss = d["loss"]
        self.config.args.weight_decay = self.weight_decay = d.get("weight_decay", self.config.args.weight_decay)

    def save_model(self, filename, d):
        Classifier.save_model(self, filename, d)
        self.finalize()
        for model in self.sub_models():
            model.save_sub_model(d)
            if self.config.args.verbose <= 3:
                self.config.print(model.params_str, level=1)
        self.config.print(self, level=1)
        print("Saving model to '%s'" % filename + ".pth")
        torch.save(self.state_dict(), filename + ".pth")

    def load_model(self, filename, d):
        self.model = None
        self.init_model()
        print("Loading model from '%s'" % filename + ".pth")
        self.load_state_dict(torch.load(filename + ".pth"))
        self.axes = OrderedDict()
        for axis, labels in self.labels_t.items():
            _, size = labels
            assert size, "Size limit for '%s' axis labels is %s" % (axis, size)
            self.axes[axis] = AxisModel(axis, size, self.config, self.model, self.birnn_type)
        self.config.print(self, level=1)

    def params_num(self, d):
        return sum(len(m.get_sub_dict(d).get("param_keys", ())) for m in self.sub_models())

    def all_params(self, as_array=True):
        d = super().all_params()
        for model in self.sub_models():
            for name, param in model.named_parameters():
                if param.requires_grad:
                    d["_".join(model.save_path + (name,))] = param
        return d

    def print_params(self, max_rows=10):
        for model in self.sub_models():
            for key, value in model.params.items():
                print("[%s] %s" % (model.params_str(), key), file=sys.stderr)
                # noinspection PyBroadException
                try:
                    print(value[:max_rows])
                except Exception:
                    pass
