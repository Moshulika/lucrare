from flask import Blueprint, Response, render_template

pages_bp = Blueprint("pages", __name__)

_FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>
<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
<stop offset='0%' stop-color='#a06bff'/><stop offset='100%' stop-color='#ff5fc6'/>
</linearGradient></defs>
<path fill='url(#g)' d='M5 13h2v7H5zm4-6h2v13H9zm4 9h2v4h-2zm4-5h2v9h-2z'/>
<circle cx='6' cy='10' r='1.6' fill='url(#g)'/>
<circle cx='10' cy='4' r='1.6' fill='url(#g)'/>
<circle cx='14' cy='13' r='1.6' fill='url(#g)'/>
<circle cx='18' cy='8' r='1.6' fill='url(#g)'/>
</svg>"""


@pages_bp.get("/")
def index():
    return render_template("index.html")


@pages_bp.get("/favicon.ico")
def favicon():
    return Response(_FAVICON_SVG, mimetype="image/svg+xml")