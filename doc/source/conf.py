#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# function-pipe documentation build configuration file, created by
# sphinx-quickstart on Fri Jan  6 16:49:22 2017.
#
# This file is execfile()d with the current directory set to its containing dir.
#
# Note that not all possible configuration values are present in this
# autogenerated file.
#
# All configuration values have a default; values that are commented out
# serve to show the default.

import sys
import os
import datetime
import io
import inspect
import typing as tp

import static_frame as sf

from static_frame.core.interface import INTERFACE_GROUP_ORDER
from static_frame.core.interface import InterfaceSummary
from static_frame.core.util import AnyCallable
from static_frame.test.unit.test_doc import api_example_str

PREFIX_START = '#start_'
PREFIX_END = '#end_'

def get_defined() -> tp.Set[str]:

    defined = set()
    signature_start = ''
    signature_end = ''

    for line in api_example_str.split('\n'):
        if line.startswith(PREFIX_START):
            signature_start = line.replace(PREFIX_START, '').strip()
        elif line.startswith(PREFIX_END):
            signature_end = line.replace(PREFIX_END, '').strip()
            if signature_start == signature_end:
                if signature_start in defined:
                    raise RuntimeError(f'duplicate definition: {signature_start}')
                defined.add(signature_start)
                signature_start = ''
                signature_end = ''
            else:
                raise RuntimeError(f'mismatched: {signature_start}: {signature_end}')

    return defined

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.

DOCUMENTED_COMPONENTS = (
        sf.Series,
        sf.SeriesHE,
        sf.Frame,
        sf.FrameGO,
        sf.FrameHE,
        sf.Bus,
        sf.Batch,
        sf.Yarn,
        sf.Quilt,
        sf.Index,
        sf.IndexGO,
        sf.IndexHierarchy,
        sf.IndexHierarchyGO,
        sf.IndexYear,
        sf.IndexYearGO,
        sf.IndexYearMonth,
        sf.IndexYearMonthGO,
        sf.IndexDate,
        sf.IndexDateGO,
        sf.IndexMinute,
        sf.IndexMinuteGO,
        sf.IndexHour,
        sf.IndexHourGO,
        sf.IndexSecond,
        sf.IndexSecondGO,
        sf.IndexMillisecond,
        sf.IndexMillisecondGO,
        sf.IndexMicrosecond,
        sf.IndexMicrosecondGO,
        sf.IndexNanosecond,
        sf.IndexNanosecondGO,
        sf.DisplayConfig,
        sf.StoreConfig,
        sf.StoreFilter,
        sf.NPZ,
        sf.NPY,
        )


def get_jinja_contexts() -> tp.Dict[str, tp.Any]:

    post: tp.Dict[str, tp.Any] = {}

    # performance_cls = []
    # for name in dir(core):
    #     obj = getattr(core, name)
    #     if inspect.isclass(obj) and issubclass(obj, PerfTest):
    #         performance_cls.append(obj.__name__)

    # post['performance_cls'] = performance_cls

    # for docs
    post['examples_defined'] = get_defined()
    # post['interface_groups'] = INTERFACE_GROUP_ORDER

    post['interface'] = {}
    for target in DOCUMENTED_COMPONENTS:
        inter = InterfaceSummary.to_frame(target, #type: ignore
                minimized=False,
                max_args=99, # +inf, but keep as int
                )
        # break into iterable of group, frame
        inter_items = []
        for g in INTERFACE_GROUP_ORDER:
            inter_sub = inter.loc[inter['group'] == g]
            if len(inter_sub): # some groups are empty
                inter_items.append((g, inter_sub))
        post['interface'][target.__name__] = (
                target.__name__,
                target,
                inter_items,
                )
    return post

jinja_contexts = {'ctx': get_jinja_contexts()}


# -- General configuration -----------------------------------------------------

# If your documentation needs a minimal Sphinx version, state it here.
#needs_sphinx = '1.0'

# Add any Sphinx extension module names here, as strings. They can be extensions
# coming with Sphinx (named 'sphinx.ext.*') or your custom ones.
extensions = [
        'sphinx.ext.autodoc',
        'sphinx.ext.viewcode',
        'sphinx.ext.graphviz',
        'sphinx.ext.inheritance_diagram',
        'sphinxcontrib.napoleon',
        'sphinxcontrib.jinja',
        ]


# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# The suffix of source filenames.
source_suffix = '.rst'

# The encoding of source files.
#source_encoding = 'utf-8-sig'

# The master toctree document.
master_doc = 'index'

# General information about the project.
project = 'StaticFrame'
copyright = '%s, Christopher Ariza' % datetime.datetime.now().year

# The version info for the project you're documenting, acts as replacement for
# |version| and |release|, also used in various other places throughout the
# built documents.
#
# The short X.Y version.
version = '.'.join(sf.__version__.split('.')[:2])
# The full version, including alpha/beta/rc tags.
release = sf.__version__

# The language for content autogenerated by Sphinx. Refer to documentation
# for a list of supported languages.
#language = None

# There are two options for replacing |today|: either, you set today to some
# non-false value, then it is used:
#today = ''
# Else, today_fmt is used as the format for a strftime call.
#today_fmt = '%B %d, %Y'

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
exclude_patterns: tp.List[str] = []

add_module_names = False
# The reST default role (used for this markup: `text`) to use for all documents.
#default_role = None

# If true, '()' will be appended to :func: etc. cross-reference text.
add_function_parentheses = True

# If true, the current module name will be prepended to all description
# unit titles (such as .. function::).
add_module_names = False

# If true, sectionauthor and moduleauthor directives will be shown in the
# output. They are ignored by default.
#show_authors = False

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'

# A list of ignored prefixes for module index sorting.
#modindex_common_prefix = []

# If true, keep warnings as "system message" paragraphs in the built documents.
#keep_warnings = False


# -- Options for HTML output ---------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
on_rtd = os.environ.get('READTHEDOCS') == 'True'
if on_rtd:
    html_theme = 'default'
else:
    import sphinx_rtd_theme
    html_theme = 'sphinx_rtd_theme'
    html_theme_path = [sphinx_rtd_theme.get_html_theme_path()]

# Theme options are theme-specific and customize the look and feel of a theme
# further.  For a list of options available for each theme, see the
# documentation.
#html_theme_options = {}

# Add any paths that contain custom themes here, relative to this directory.
#html_theme_path = []

# The name for this set of Sphinx documents.  If None, it defaults to
# "<project> v<release> documentation".
#html_title = None

# A shorter title for the navigation bar.  Default is the same as html_title.
#html_short_title = None

# The name of an image file (relative to this directory) to place at the top
# of the sidebar.
#html_logo = None

# The name of an image file (within the static path) to use as favicon of the
# docs.  This file should be a Windows icon file (.ico) being 16x16 or 32x32
# pixels large.
html_favicon = '../images/favicon.ico'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
# html_static_path = ['_static']

# If not '', a 'Last updated on:' timestamp is inserted at every page bottom,
# using the given strftime format.
html_last_updated_fmt = '%b %d, %Y'

# If true, SmartyPants will be used to convert quotes and dashes to
# typographically correct entities.
#html_use_smartypants = True

# Custom sidebar templates, maps document names to template names.
#html_sidebars = {}

# Additional templates that should be rendered to pages, maps page names to
# template names.
#html_additional_pages = {}

# If false, no module index is generated.
#html_domain_indices = True

# If false, no index is generated.
#html_use_index = True

# If true, the index is split into individual pages for each letter.
#html_split_index = False

# If true, links to the reST sources are added to the pages.
#html_show_sourcelink = True

# If true, "Created using Sphinx" is shown in the HTML footer. Default is True.
#html_show_sphinx = True

# If true, "(C) Copyright ..." is shown in the HTML footer. Default is True.
#html_show_copyright = True

# If true, an OpenSearch description file will be output, and all pages will
# contain a <link> tag referring to it.  The value of this option must be the
# base URL from which the finished HTML is served.
#html_use_opensearch = ''

# This is the file name suffix for HTML files (e.g. ".xhtml").
#html_file_suffix = None

# Output file base name for HTML help builder.
htmlhelp_basename = 'static-frame'


# -- Options for LaTeX output --------------------------------------------------

latex_elements: tp.Dict[str, str] = {
# The paper size ('letterpaper' or 'a4paper').
#'papersize': 'letterpaper',

# The font size ('10pt', '11pt' or '12pt').
#'pointsize': '10pt',

# Additional stuff for the LaTeX preamble.
#'preamble': '',
}

# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title, author, documentclass [howto/manual]).
# latex_documents = []

# The name of an image file (relative to this directory) to place at the top of
# the title page.
#latex_logo = None

# For "manual" documents, if this is true, then toplevel headings are parts,
# not chapters.
#latex_use_parts = False

# If true, show page references after internal links.
#latex_show_pagerefs = False

# If true, show URL addresses after external links.
#latex_show_urls = False

# Documents to append as an appendix to all manuals.
#latex_appendices = []

# If false, no module index is generated.
#latex_domain_indices = True


# -- Options for manual page output --------------------------------------------

# One entry per manual page. List of tuples
# (source start file, name, description, authors, manual section).
man_pages: tp.List[tp.Tuple[str, str, str, str, str]] = []

# If true, show URL addresses after external links.
#man_show_urls = False


# -- Options for Texinfo output ------------------------------------------------

# Grouping the document tree into Texinfo files. List of tuples
# (source start file, target name, title, author,
#  dir menu entry, description, category)
texinfo_documents: tp.List[tp.Tuple[str, str, str, str, str, str, str]] = []

# Documents to append as an appendix to all manuals.
#texinfo_appendices = []

# If false, no module index is generated.
#texinfo_domain_indices = True

# How to display URL addresses: 'footnote', 'no', or 'inline'.
#texinfo_show_urls = 'footnote'

# If true, do not generate a @detailmenu in the "Top" node's menu.
#texinfo_no_detailmenu = False
