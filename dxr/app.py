from logging import StreamHandler
from os.path import isdir, isfile, join
from sys import stderr
from time import time
from urllib import quote_plus

from flask import (Blueprint, Flask, send_from_directory, current_app,
                   send_file, request, redirect, jsonify, render_template)

from dxr.query import Query
from dxr.server_utils import connect_db
from dxr.utils import non_negative_int, search_url, sqlite3  # Make sure we load trilite before possibly importing the wrong version of sqlite3.


# Look in the 'dxr' package for static files, templates, etc.:
dxr_blueprint = Blueprint('dxr_blueprint', 'dxr')


def make_app(instance_path):
    """Return a DXR application which looks in the given folder for
    configuration.

    Also set up the static and template folder according to the configured
    template.

    """
    # TODO: Actually obey the template selection in the config file by passing
    # a different static_folder and template_folder to Flask().
    app = Flask('dxr', instance_path=instance_path)
    app.register_blueprint(dxr_blueprint)

    # Load the special config file generated by dxr-build:
    app.config.from_pyfile(join(app.instance_path, 'config.py'))

    # Log to Apache's error log in production:
    app.logger.addHandler(StreamHandler(stderr))
    return app


@dxr_blueprint.route('/')
def index():
    config = current_app.config
    wwwroot = config['WWW_ROOT']
    tree = config['TREES'][0]
    return redirect('%s/%s/source/' % (wwwroot, tree))


@dxr_blueprint.route('/<tree>/search')
def search(tree):
    """Search by regex, caller, superclass, or whatever."""
    # TODO: This function still does too much.
    querystring = request.values

    offset = non_negative_int(querystring.get('offset'), 0)
    limit = min(non_negative_int(querystring.get('limit'), 100), 1000)

    config = current_app.config

    # Arguments for the template:
    arguments = {
        # Common template variables
        'wwwroot': config['WWW_ROOT'],
        'tree': config['TREES'][0],
        'trees': config['TREES'],
        'config': config['TEMPLATE_PARAMETERS'],
        'generated_date': config['GENERATED_DATE']}

    error = warning = ''
    status_code = None

    if tree in config['TREES']:
        arguments['tree'] = tree

        # Connect to database
        conn = connect_db(tree, current_app.instance_path)
        if conn:
            # Parse the search query
            qtext = querystring.get('q', '')
            is_case_sensitive = querystring.get('case') == 'true'
            q = Query(conn,
                      qtext,
                      should_explain='explain' in querystring,
                      is_case_sensitive=is_case_sensitive)

            # Try for a direct result:
            if querystring.get('redirect') == 'true':
                result = q.direct_result()
                if result:
                    path, line = result
                    # TODO: Does this escape qtext properly?
                    return redirect(
                        '%s/%s/source/%s?from=%s%s#%i' %
                        (config['WWW_ROOT'],
                         tree,
                         path,
                         qtext,
                         '&case=true' if is_case_sensitive else '', line))

            # Return multiple results:
            template = 'search.html'
            start = time()
            try:
                results = list(q.results(offset, limit))
            except sqlite3.OperationalError as e:
                if e.message.startswith('REGEXP:'):
                    # Malformed regex
                    warning = e.message[7:]
                    results = []
                elif e.message.startswith('QUERY:'):
                    warning = e.message[6:]
                    results = []
                else:
                    error = 'Database error: %s' % e.message
            if not error:
                # Search template variables:
                arguments['time'] = time() - start
                arguments['query'] = qtext
                arguments['search_url'] = search_url(arguments['wwwroot'],
                                                     arguments['tree'],
                                                     qtext,
                                                     redirect=False)
                arguments['results'] = results
                arguments['offset'] = offset
                arguments['limit'] = limit
                arguments['is_case_sensitive'] = is_case_sensitive
        else:
            error = 'Failed to establish database connection.'
    else:
        error = "Tree '%s' is not a valid tree." % tree
        status_code = 404

    if warning or error:
        arguments['error'] = error or warning

    if querystring.get('format') == 'json':
        if error:
            # Return a non-OK code so the live search doesn't try to replace
            # the results with our empty ones:
            return jsonify(arguments), status_code or 500

        # Tuples are encoded as lists in JSON, and these are not real
        # easy to unpack or read in Javascript. So for ease of use, we
        # convert to dictionaries before returning the json results.
        # If further discrepancies are introduced, please document them in
        # templating.mkd.
        arguments['results'] = [
            {'icon': icon,
             'path': path,
             'lines': [{'line_number': nb, 'line': l} for nb, l in lines]}
                for icon, path, lines in arguments['results']]
        return jsonify(arguments)

    if error:
        return render_template('error.html', **arguments), status_code or 500
    else:
        return render_template('search.html', **arguments)


@dxr_blueprint.route('/<tree>/source/')
@dxr_blueprint.route('/<tree>/source/<path:path>')
def browse(tree, path=''):
    """Show a directory listing or a single file from one of the trees."""
    tree_folder = _tree_folder(tree)
    return send_from_directory(tree_folder, _html_file_path(tree_folder, path))


@dxr_blueprint.route('/<tree>/')
@dxr_blueprint.route('/<tree>')
def tree_root(tree):
    """Redirect requests for the tree root instead of giving 404s."""
    return redirect(tree + '/source/')


@dxr_blueprint.route('/<tree>/parallel/')
@dxr_blueprint.route('/<tree>/parallel/<path:path>')
def parallel(tree, path=''):
    """If a file or dir parallel to the given path exists in the given tree,
    redirect to it. Otherwise, redirect to the root of the given tree.

    We do this with the future in mind, in which pages may be rendered at
    request time. To make that fast, we wouldn't want to query every one of 50
    other trees, when drawing the Switch Tree menu, to see if a parallel file
    or folder exists. So we use this controller to put off the querying until
    the user actually choose another tree.

    """
    tree_folder = _tree_folder(tree)
    disk_path = _html_file_path(tree_folder, path)
    www_root = current_app.config['WWW_ROOT']
    if isfile(join(tree_folder, disk_path)):
        return redirect('{root}/{tree}/source/{path}'.format(
            root=www_root,
            tree=tree,
            path=path))
    else:
        return redirect('{root}/{tree}/source/'.format(
            root=www_root,
            tree=tree))


def _tree_folder(tree):
    """Return the on-disk path to the root of the given tree's folder in the
    instance."""
    return join(current_app.instance_path, 'trees', tree)


def _html_file_path(tree_folder, url_path):
    """Return the on-disk path, relative to the tree folder, of the HTML file
    that should be served when a certain path is browsed to.

    :arg tree_folder: The on-disk path to the tree's folder in the instance
    :arg url_path: The URL path browsed to, rooted just inside the tree

    If you provide a path to a non-existent file or folder, I will happily
    return a path which has no corresponding FS entity.

    """
    if isdir(join(tree_folder, url_path)):
        # It's a bare directory. Add the index file to the end:
        return join(url_path, current_app.config['DIRECTORY_INDEX'])
    else:
        # It's a file. Add the .html extension:
        return url_path + '.html'
