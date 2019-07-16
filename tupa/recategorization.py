import re
import string
from operator import attrgetter

from word2number import w2n

from tupa.constraints.amr import read_resources, CATEGORIES, NEGATIONS, VERBALIZATION, MONTHS

NUM_PATTERN = re.compile(r"[+-]?\d+(\.\d+)?")
TOKEN_PLACEHOLDER = "<t>"
TOKEN_TITLE_PLACEHOLDER = "<T>"
LEMMA_PLACEHOLDER = "<l>"
NEGATION_PLACEHOLDER = "<n>"
CATEGORY_SEPARATOR = "|"  # after the separator there is the label category
PUNCTUATION_REMOVER = str.maketrans("", "", string.punctuation)


def resolve(node, value, introduce_placeholders=False, conservative=False, is_node_label=True):
    """
    Replace any placeholder in node label/property with the corresponding terminals' text, and remove category suffix
    :param node: node whose label or property value is to be resolved
    :param value: the label or property value to resolve
    :param introduce_placeholders: if True, *introduce* placeholders and categories into the label rather than resolving
    :param conservative: avoid replacement when risky due to multiple terminal children that could match
    :param is_node_label: is this a node label (not property value)
    :return: the resolved label, with or without placeholders and categories (depending on the value of reverse)
    """
    def _replace(old, new):  # replace only inside the label value/name
        new = new.strip('"()')
        if introduce_placeholders:
            old, new = new, old
        replaceable = old and (len(old) > 2 or len(value) < 5)
        return re.sub(re.escape(old) + r"(?![^<]*>|[^(]*\(|\d+$)", new, value, 1) if replaceable else value

    read_resources()

    if value is not None:
        category = None
        if introduce_placeholders:
            category = CATEGORIES.get(value)  # category suffix to append to label
        elif CATEGORY_SEPARATOR in value:
            value = value[:value.find(CATEGORY_SEPARATOR)]  # remove category suffix
        terminals = sorted([c for c in node.children if getattr(c, "text", None)], key=attrgetter("index"))
        if terminals:
            if not introduce_placeholders and NUM_PATTERN.match(value):  # numeric
                number = terminals_to_number(terminals)  # try replacing spelled-out numbers/months with digits
                if number is not None:
                    value = str(number)
            else:
                if len(terminals) > 1:
                    if introduce_placeholders or value.count(TOKEN_PLACEHOLDER) == 1:
                        value = _replace(TOKEN_PLACEHOLDER, "".join(t.text for t in terminals))
                    if introduce_placeholders or value.count(TOKEN_TITLE_PLACEHOLDER) == 1:
                        value = _replace(TOKEN_TITLE_PLACEHOLDER, "_".join(merge_punct(t.text for t in terminals)))
                    if conservative:
                        terminals = ()
                for terminal in terminals:
                    lemma = lemmatize(terminal)
                    if lemma:
                        if introduce_placeholders and category is None:
                            category = CATEGORIES.get(lemma)
                        value = _replace(LEMMA_PLACEHOLDER, lemma)
                    value = _replace(TOKEN_PLACEHOLDER, terminal.text)
                    value = _replace(TOKEN_TITLE_PLACEHOLDER, terminal.text.title())
                    negation = NEGATIONS.get(terminal.text)
                    if negation is not None:
                        value = _replace(NEGATION_PLACEHOLDER, negation)
                    if is_node_label:
                        morph = VERBALIZATION.get(lemma)
                        if morph:
                            for prefix, value in morph.items():  # V: verb, N: noun, A: noun actor
                                value = _replace("<%s>" % prefix, value)
        if introduce_placeholders and category:
            value += CATEGORY_SEPARATOR + category
    return value


def terminals_to_number(terminals):
    text = " ".join(t.text for t in terminals)
    try:  # first make sure it's not a number already
        float(text)
        return None
    except ValueError:
        pass
    # noinspection PyBroadException
    try:
        return w2n.word_to_num(text)
    except Exception:
        pass
    if len(terminals) == 1:
        try:
            return MONTHS.index(terminals[0].text.lower()) + 1
        except ValueError:
            pass


def lemmatize(terminal):
    lemma = terminal.get("lemma")
    if lemma == "-PRON-":
        lemma = terminal.text
    return lemma.translate(PUNCTUATION_REMOVER).lower() if lemma else None


def merge_punct(tokens):
    """
    If a token starts/ends with punctuation, merge it with the previous/next token
    """
    ret = list(tokens)
    while len(ret) > 1:
        for i, token in enumerate(ret):
            s, e = i, i + 1
            if len(token):
                if e < len(ret) and token.endswith(tuple(string.punctuation)):
                    e += 1
                if s and token.startswith(tuple(string.punctuation)):
                    s -= 1
            if s + 1 < e:
                ret[s:e] = ["".join(ret[s:e])]
                break
        else:
            break
    return ret