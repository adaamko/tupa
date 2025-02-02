import os
from base64 import b64encode
from io import BytesIO
from urllib.parse import quote
from xml.etree.ElementTree import fromstring, tostring

import flask_assets
import jinja2
import matplotlib
from flask import Flask, render_template, Response, request
from flask_compress import Compress
from ucca import layer1
from ucca.convert import from_text, to_standard, from_standard
from ucca.textutil import indent_xml
from ucca.visualization import draw
from webassets import Environment as AssetsEnvironment
from webassets.ext.jinja2 import AssetsExtension

from semstr.convert import TO_FORMAT
from tupa.parse import Parser

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg

SCRIPT_DIR = os.path.dirname(__file__)

app = Flask(__name__)
assets = flask_assets.Environment()
assets.init_app(app)
assets_env = AssetsEnvironment("./static/", "/static")
jinja_environment = jinja2.Environment(
    autoescape=True,
    loader=jinja2.FileSystemLoader(os.path.join(SCRIPT_DIR, "templates")),
    extensions=[AssetsExtension])
jinja_environment.assets_environment = assets_env
Compress(app)

PARSER_MODEL = os.getenv("PARSER_MODEL", os.path.join(SCRIPT_DIR, "..", "models/ucca-bilstm"))
app_parser = Parser(PARSER_MODEL)


@app.route("/")
def parser_demo():
    return render_template("demo.html")


@app.route("/parse", methods=["POST"])
def parse():
    request_data = request.get_json()
    text = request_data["text"]
    print("Parsing text: '%s'" % text)
    in_passage = next(from_text(text))
    out_passage = next(app_parser.parse(in_passage))[0]
    root = to_standard(out_passage)
    xml = tostring(root).decode()
    return Response(indent_xml(xml), headers={"Content-Type": "xml/application"})

@app.route("/parse_demo", methods=["POST"])
def parse_demo():
    text = request.values["input"]
    print("Parsing text: '%s'" % text)
    in_passage = next(from_text(text))
    out_passage = next(app_parser.parse(in_passage))[0]
    root = to_standard(out_passage)
    xml = tostring(root).decode()
    return Response(indent_xml(xml), headers={"Content-Type": "xml/application"})




@app.route("/visualize", methods=["POST"])
def visualize():
    xml = request.get_data()
    passage = from_standard(fromstring(xml))
    print("Visualizing passage %s: %s" % (passage.ID, passage.layer(layer1.LAYER_ID).heads[0]))
    fig = plt.figure()
    canvas = FigureCanvasAgg(fig)
    draw(passage)
    image = BytesIO()
    canvas.print_png(image)
    data = b64encode(image.getvalue()).decode()
    plt.close()
    return Response(quote(data.rstrip("\n")))

CONTENT_TYPES = {"xml": "xml/application", "json": "application/json"}


@app.route("/download", methods=["POST"])
def download():
    xml = request.values["input"]
    out_format = request.values["format"]
    print("Converting to " + out_format)
    out = xml if out_format == "xml" else "\n".join(TO_FORMAT[out_format](from_standard(fromstring(xml))))
    return Response(out, headers={"Content-Type": CONTENT_TYPES.get(out_format, "text/plain")})


session_opts = {
    "session.type": "file",
    "session.cookie_expires": 60 * 24 * 60 * 2,  # two days in seconds
    "session.data_dir": "./data",
    "session.auto": True
}

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv("PORT", 5001)))
