import os
import sys
import time

# -- Path setup ------------------------------------------------------------------------

sys.path.insert(0, os.path.abspath("../src"))

# -- Project information ---------------------------------------------------------------

# Read author and version from the package itself so the docs never go stale.
from bora import __author__, __version__  # noqa: E402

author = __author__
version = __version__
release = version
project = "Bora"
title = "Bora API Documentation"
copyright = f"{time.localtime().tm_year}, {author}"

# -- General configuration -------------------------------------------------------------

extensions = [
    "sphinx.ext.viewcode",
    "sphinxext.opengraph",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx.ext.todo",
    "sphinx.ext.intersphinx",
    "autoapi.extension",
    "sphinx_mdinclude",
]
source_suffix = [".rst", ".md"]
master_doc = "index"
language = "en"
# html4_writer = True

# -- Options for HTML output -----------------------------------------------------------

html_theme = "furo"
html_logo = "bora.png"
html_static_path = ["_static"]
html_css_files = [
    "css/custom.css",
]

# -- Options for LaTeX output ----------------------------------------------------------

latex_documents = [
    (master_doc, "bora.tex", title, author, "manual"),
]

# -- Extension configuration -----------------------------------------------------------

# sphinx.ext.autodoc
autodoc_typehints = "description"
autoclass_content = "both"
autodoc_member_order = "bysource"
autodoc_inherit_docstrings = False

# autoapi.extension
autoapi_type = "python"
autoapi_dirs = ["../src/bora"]
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
]
autoapi_add_toctree_entry = False

# sphinx.ext.todo
todo_include_todos = True

# sphinx.ext.intersphsinx
intersphinx_mapping = {
    "click": ("https://click.palletsprojects.com/en/latest/", None),
    "fasteners": ("https://fasteners.readthedocs.io/en/latest/", None),
    "keyring": ("https://keyring.readthedocs.io/en/latest/", None),
    "Pyro5": ("https://pyro5.readthedocs.io/en/latest/", None),
    "python": ("https://docs.python.org/3/", None),
    "requests": ("https://docs.python-requests.org/en/master/", None),
    "watchdog": ("https://python-watchdog.readthedocs.io/en/latest/", None),
}
