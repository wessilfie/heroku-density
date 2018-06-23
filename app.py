import datetime
from functools import wraps
import json
import re
import traceback

from bokeh.resources import CDN
from flask import Flask, g, jsonify, render_template, request
from flask_caching import Cache
import httplib2
from oauth2client.client import flow_from_clientsecrets
import psycopg2
import psycopg2.extras
import psycopg2.pool
from werkzeug.contrib.cache import SimpleCache

import db, librarytimes
import graphics
from config import config, ISO8601Encoder
from data import FULL_CAP_DATA
from predict import db_to_pandas, predict_today
import os

app = Flask(__name__)
cache = SimpleCache()
# change the default JSON encoder to handle datetime's properly
app.json_encoder = ISO8601Encoder

CU_EMAIL_REGEX = r"^(?P<uni>[a-z\d]+)@.*(columbia|barnard)\.edu$"
request_date_format = '%Y-%m-%d'

# create a pool of postgres connections
pg_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=5, maxconn=20, dsn=os.environ['DATABASE_URL'])


@app.before_request
def get_connections():
    """ Get connections from the Postgres pool. """
    g.pg_conn = pg_pool.getconn()
    g.cursor = g.pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    g.start_time = datetime.datetime.now()


def return_connections():
    """ Return the connection to the Postgres connection pool. """
    g.cursor.close()
    pg_pool.putconn(g.pg_conn)


def unsafe_date(*keys):
    """ Takes URL parameters to filter as an argument """

    def dec(fn):
        @wraps(fn)
        def validate_datetime(*args, **kwargs):
            for dt in keys:
                arg_date = request.view_args.get(dt)
                try:
                    datetime.datetime.strptime(arg_date, request_date_format)
                except ValueError:
                    return jsonify(
                        error=("Invalid datetime format, "
                               "'{}', please use YYYY-MM-DD format")
                        .format(arg_date)), 400

            return fn(*args, **kwargs)

        return validate_datetime

    return dec


@app.after_request
def log_outcome(resp):
    """ Outputs to a specified logging file """
    # return db connections first
    g.pg_conn.commit()
    return_connections()
    # TODO: log the request and its outcome
    return resp


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html')


if not app.debug:

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def internal_error(e):
        traceback.print_exc()
        return jsonify(
            error="Something went wrong, and notification of "
            "admins failed.  Please contact an admin.",
            error_data=traceback.format_exc())


def authorization_required(func):
    @wraps(func)
    def authorization_checker(*args, **kwargs):
        token = request.headers.get('Authorization-Token')
        if not token:
            token = request.args.get('auth_token')
            if not token:
                response = jsonify(error="No authorization token provided")
                response.status_code = 401  # unauthorized
                return response

        uni = db.get_uni_for_code(g.cursor, token)
        if not uni:
            response = jsonify(error="No authorization token provided")
            response.status_code = 401  # unauthorized
            return response

        # TODO: Some logging right here. We can log which user is using what.
        return func(*args, **kwargs)

    return authorization_checker


def annotate_fullness_percentage(data):
    """
    Calculates percent fullness of all groups and adds them to the data in
    the key 'percent_full'. The original data is not modified.
    :param list of dictionaries cur_data: data to calculate fullness percentage
    :return: list of dictionaries with added pecent_full data
    :rtype: list of dictionaries
    """
    groups = []
    for row in data:
        capacity = FULL_CAP_DATA[row["group_name"]]
        percent = (100 * row["client_count"]) // capacity

        copy = dict(**row)
        copy["percent_full"] = min(100, percent)
        groups.append(copy)
    return groups


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/docs')
def docs():
    return render_template('docs.html')


@app.route('/docs/building_info')
def building_info():
    """
    Gets a json with the group ids, group names, parent ids, and parent names
    """

    fetched_data = db.get_building_info(g.cursor)

    return jsonify(data=fetched_data)


@app.route('/auth')
def auth():
    """
    Returns an auth code after user logs in through Google+.
    :param string code: code that is passed in through Google+.
                        Do not provide this yourself.
    :return: An html page with an auth code.
    :rtype: flask.Response
    """

    # Get code from params.
    code = request.args.get('code')
    print(code)
    if not code:
        return render_template('auth.html', success=False)

    try:
        # Exchange code for email address.
        # Get Google+ ID.
        oauth_flow = flow_from_clientsecrets('client_secrets.json', scope='')
        print(oauth_flow)
        print("trying")
        oauth_flow.redirect_uri = 'postmessage'
        credentials = oauth_flow.step2_exchange(code)
        print(credentials)
        gplus_id = credentials.id_token['sub']
        print(gplus_id)

        # Get first email address from Google+ ID.
        http = httplib2.Http()
        http = credentials.authorize(http)

        h, content = http.request(
            'https://www.googleapis.com/plus/v1/people/' + gplus_id, 'GET')
        data = json.loads(content)
        print(data)
        email = data["emails"][0]["value"]

        # Verify email is valid.
        regex = re.match(CU_EMAIL_REGEX, email)

        if not regex:
            return render_template(
                'auth.html',
                success=False,
                reason="Please log in with your " +
                "Columbia or Barnard email. You logged " + "in with: " + email)

        # Get UNI and ask database for code.
        uni = regex.group('uni')
        code = db.get_oauth_code_for_uni(g.cursor, uni)
        return render_template('auth.html', success=True, uni=uni, code=code)
    except psycopg2.IntegrityError:
        return render_template(
            'auth.html',
            success=False,
            reason="Attempt to generate API key resulted in"
            " a collision with another key in the"
            " database. Please refresh to try and"
            " generate a new key.")

    except Exception as e:
        # TODO: log errors
        print(e)
        return render_template(
            'auth.html',
            success=False,
            reason="An error occurred. Please try again.")


@app.route('/latest')
@authorization_required
def get_latest_data():
    """
    Gets latest dump of data for all endpoints.
    :return: Latest JSON
    :rtype: flask.Response
    """
    fetched_data = db.get_latest_data(g.cursor)

    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    return jsonify(data=fetched_data)


@app.route('/latest/group/<group_id>')
@authorization_required
def get_latest_group_data(group_id):
    """
    Gets latest dump of data for the specified group.
    :param int group_id: id of the group requested
    :return: Latest JSON corresponding to the requested group
    :rtype: flask.Response
    """

    fetched_data = db.get_latest_group_data(g.cursor, group_id)

    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    return jsonify(data=fetched_data)


@app.route('/latest/building/<parent_id>')
@authorization_required
def get_latest_building_data(parent_id):
    """
    Gets latest dump of data for the specified building.
    :param int parent_id: id of the building requested
    :return: Latest JSON corresponding to the requested building
    :rtype: flask.Response
    """

    fetched_data = db.get_latest_building_data(g.cursor, parent_id)

    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    return jsonify(data=fetched_data)


@app.route('/day/<day>/group/<group_id>')
@unsafe_date('day')
@authorization_required
def get_day_group_data(day, group_id):
    """
    Gets specified group data for specified day
    :param str day: the day requested in EST format YYYY-MM-DD
    :param int group_id: id of the group requested
    :return: JSON corresponding to the requested day and group
    :rtype: flask.Response
    """

    # Convert to datetime object
    start_day = datetime.datetime.strptime(day, "%Y-%m-%d")
    end_day = start_day + datetime.timedelta(days=1)

    fetched_data = db.get_window_based_on_group(
        g.cursor, group_id, start_day, end_day, offset=0)
    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    return jsonify(data=fetched_data)


@app.route('/day/<day>/building/<parent_id>')
@unsafe_date('day')
@authorization_required
def get_day_building_data(day, parent_id):
    """
    Gets specified building data for specified day
    :param str day: the day requested in EST format YYYY-MM-DD
    :param int parent_id: id of the building requested
    :return: JSON corresponding to the requested day and building
    :rtype: flask.Response
    """

    # Convert to datetime object
    start_day = datetime.datetime.strptime(day, "%Y-%m-%d")
    end_day = start_day + datetime.timedelta(days=1)

    fetched_data = db.get_window_based_on_parent(
        g.cursor, parent_id, start_day, end_day, offset=0)

    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    return jsonify(data=fetched_data)


@app.route('/window/<start_time>/<end_time>/group/<group_id>')
@authorization_required
def get_window_group_data(start_time, end_time, group_id):
    """
    Gets specified group data split by the specified time delimiter.
    :param str start_time: start time in EST format YYYY-MM-DDThh:mm
    :param str end_time: end time in EST format YYYY-MM-DDThh:mm
    :param int group_id: id of the group requested
    :return: JSON corresponding to the requested window and group
    :rtype: flask.Response
    """
    offset = request.args.get(
        'offset', type=int) if request.args.get('offset') else 0
    fetched_data = db.get_window_based_on_group(g.cursor, group_id, start_time,
                                                end_time, offset)
    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    next_page_url = None
    if len(fetched_data) == db.QUERY_LIMIT:
        new_offset = offset + db.QUERY_LIMIT
        next_page_url = request.base_url + '?auth_token=' + request.args.get(
            'auth_token') + '&offset=' + str(new_offset)

    return jsonify(data=fetched_data, next_page=next_page_url)


@app.route('/window/<start_time>/<end_time>/building/<parent_id>')
@authorization_required
def get_window_building_data(start_time, end_time, parent_id):
    """
    Gets specified building data split by the specified time delimiter.
    :param str start_time: start time in EST format YYYY-MM-DDThh:mm
    :param str end_time: end time in EST format YYYY-MM-DDThh:mm
    :param int parent_id: id of the building requested
    :return: JSON corresponding to the requested window and building
    :rtype: flask.Response
    """
    offset = request.args.get(
        'offset', type=int) if request.args.get('offset') else 0
    fetched_data = db.get_window_based_on_parent(g.cursor, parent_id,
                                                 start_time, end_time, offset)
    # Add percentage_full
    fetched_data = annotate_fullness_percentage(fetched_data)

    next_page_url = None
    if len(fetched_data) == db.QUERY_LIMIT:
        new_offset = offset + db.QUERY_LIMIT
        next_page_url = request.base_url + '?auth_token=' + request.args.get(
            'auth_token') + '&offset=' + str(new_offset)
    return jsonify(data=fetched_data, next_page=next_page_url)


@app.route('/')
def capacity():
    """Render and show capacity page"""
    cur_data = db.get_latest_data(g.cursor)
    last_updated = cur_data[0]['dump_time'].strftime("%B %d %Y, %I:%M %p")
    locations = annotate_fullness_percentage(cur_data)
    # times = {'Lerner 1' : 1200, 'Lerner 2' : 1300}
    times = librarytimes.dict_for_time()
    return render_template(
        'capacity.html', locations=locations,
        last_updated=last_updated, times=times)


@app.route('/map')
def map():
    cur_data = db.get_latest_data(g.cursor)
    locations = annotate_fullness_percentage(cur_data)

    # Render template has an SVG image whose colors are changed by % full
    return render_template('map.html', locations=locations)


@app.route('/predict')
def predict():
    # loading data from current database connection
    data = cache.get('predictionData')
    print(data)

    if data is None:
        data = db_to_pandas(g.cursor)
        cache.set('predictionData', data, timeout=5 * 60)

    tmrw_pred = cache.get('predictionToday')
    print(tmrw_pred)
    if tmrw_pred is None:
        tmrw_pred = predict_today(data)
        cache.set('predictionToday', tmrw_pred, timeout=5 * 60)

    # make plots from predictions
    script, divs = graphics.create_all_buildings(today_pred.transpose())

    return render_template('predict.html', divs=divs,
                           script=script, css_script=CDN.render_js())


@app.route('/upload', methods=['POST'])
def upload():
    """ Accept POST requests from CUIT to add new data to the server """
    # This is stored in local settings and is the way we verify uploads.
    if request.args.get('key') != config['UPLOAD_KEY']:
        return 'Please include a valid API key.', 401

    try:
        db.insert_density_data(g.cursor, request.get_json())
    except Exception as e:
        # TODO: proper logging
        print(e)
        return 'At least one of the records was not inserted. \
            Please contact someone in ADI for more details.', 500

    return 'Data successfully uploaded.', 200