# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
# import os
# import sys
# sys.path.insert(0, os.path.abspath('..'))


# -- Project information -----------------------------------------------------

project = 'MACE'
copyright = '2026, Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran'
author = 'Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran'


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones. CVA6 reads Markdown through recommonmark; myst_parser is its
# maintained successor. githubpages writes the .nojekyll file GitHub Pages
# needs to serve the _static directory.
extensions = [
    'myst_parser',
    'sphinx.ext.githubpages',
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
# The standalone notes kept in docs/ are not part of the site.
exclude_patterns = ['_build', '**/build', 'Thumbs.db', '.DS_Store', 'README.md',
                    'TECHNICAL_GUIDE.md', 'chia_tailnet_issue_draft.md', 'gcp_dispatch_bug.md']


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'
pygments_style = 'monokai'

# Theme options are theme-specific and customize the look and feel of a theme
# further.  For a list of options available for each theme, see the
# documentation.
#
html_theme_options = {'style_nav_header_background': '#DDDDDD'}
# html_logo = '_static/mace-logo.svg'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']

# Add custom CSS and JS files
html_css_files = ['theme_overrides.css']
html_js_files = []

master_doc = 'index'
