#!/usr/bin/env python3
"""
server.py
Flask backend for Earth Engine map tiles and timeseries.

Environment variables expected:
- EE_SERVICE_ACCOUNT : service account email (from client_email in JSON)
- EE_KEY_FILE        : path to JSON key file (optional if EE_KEY_JSON used)
- EE_KEY_JSON        : (optional) full JSON content of key; server will write to temp file
- EE_PROJECT         : GCP project id (from project_id in JSON)
- API_KEY            : a random string the frontend will send in x-api-key
- ALLOWED_ORIGINS    : CORS origin(s) (default '*')
"""
# safe_key_loader.py (insert at top of server.py, before EE_KEY_FILE usage)
import os, tempfile, base64, json

def write_key_file_from_env():
    # If EE_KEY_FILE already set, do nothing
    if os.environ.get('EE_KEY_FILE'):
        return os.environ.get('EE_KEY_FILE')

    # EE_KEY_JSON: raw JSON as string (multiline)
    ee_json = os.environ.get('EE_KEY_JSON')
    if ee_json:
        # If the UI inserted literal '\n' characters (i.e. backslash-n) try unescaping first
        if "\\n" in ee_json and not ee_json.strip().startswith("{"):
            try:
                unescaped = ee_json.encode('utf-8').decode('unicode_escape')
                # quick validation
                json.loads(unescaped)
                ee_json = unescaped
            except Exception:
                # leave ee_json as-is; try to parse below and fail if invalid
                pass
        # write to temp file
        tf = tempfile.NamedTemporaryFile('w', delete=False, suffix='.json')
        tf.write(ee_json)
        tf.flush(); tf.close()
        try: os.chmod(tf.name, 0o600)
        except Exception: pass
        os.environ['EE_KEY_FILE'] = tf.name
        return tf.name

    # EE_KEY_JSON_B64: base64 encoded JSON
    ee_json_b64 = os.environ.get('EE_KEY_JSON_B64')
    if ee_json_b64:
        try:
            data = base64.b64decode(ee_json_b64)
            # validate
            json.loads(data)
            tf = tempfile.NamedTemporaryFile('wb', delete=False, suffix='.json')
            tf.write(data)
            tf.flush(); tf.close()
            try: os.chmod(tf.name, 0o600)
            except Exception: pass
            os.environ['EE_KEY_FILE'] = tf.name
            return tf.name
        except Exception as ex:
            raise RuntimeError('Invalid EE_KEY_JSON_B64: ' + str(ex))

    return None

# call loader early
write_key_file_from_env()
import os
import time
import tempfile
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, abort
import ee

# ------------------- handle EE_KEY_JSON -> write file if provided -------------------
# If EE_KEY_JSON is provided (e.g., via Secret Manager), write it to a temp file and set EE_KEY_FILE.
if not os.environ.get('EE_KEY_FILE') and os.environ.get('EE_KEY_JSON'):
    tmpf = tempfile.NamedTemporaryFile('w', delete=False, suffix='.json')
    tmpf.write(os.environ.get('EE_KEY_JSON'))
    tmpf.flush()
    tmpf.close()
    os.environ['EE_KEY_FILE'] = tmpf.name
    try:
        os.chmod(tmpf.name, 0o600)
    except Exception:
        pass
# ------------------------------------------------------------------------------

EE_SERVICE_ACCOUNT = os.environ.get('EE_SERVICE_ACCOUNT')
EE_KEY_FILE = os.environ.get('EE_KEY_FILE')
EE_PROJECT = os.environ.get('EE_PROJECT')
API_KEY = os.environ.get('API_KEY')
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', '*')

if not EE_SERVICE_ACCOUNT or not EE_KEY_FILE:
    raise RuntimeError('EE_SERVICE_ACCOUNT and EE_KEY_FILE (or EE_KEY_JSON) must be set as environment variables.')

# Initialize Earth Engine
try:
    credentials = ee.ServiceAccountCredentials(EE_SERVICE_ACCOUNT, EE_KEY_FILE)
    ee.Initialize(credentials, project=EE_PROJECT)
except Exception as ex:
    raise RuntimeError('Failed to initialize Earth Engine: ' + str(ex))

app = Flask(__name__)

# small in-memory cache for map tokens
CACHE = {}

def cache_get(key):
    e = CACHE.get(key)
    if not e:
        return None
    # TTL 2 hours
    if time.time() - e['created_at'] > 7200:
        CACHE.pop(key, None)
        return None
    return e

def cache_set(key, value):
    value['created_at'] = time.time()
    CACHE[key] = value

# simple API key decorator
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('x-api-key') or request.args.get('api_key')
        if API_KEY and key != API_KEY:
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# minimal CORS
@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = ALLOWED_ORIGINS
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, x-api-key'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

# Build visualization image based on index
def build_visual_image(start_date, end_date, index='NDVI'):
    col = (ee.ImageCollection('COPERNICUS/S2_SR')
           .filterDate(start_date, end_date)
           .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60)))
    img = col.median()
    if index.upper() == 'NDVI':
        nd = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
        vis = {'min': -0.5, 'max': 0.8, 'palette': ['#2d004b','#3b0f70','#1a9850','#a6d96a','#ffffbf']}
        return nd, vis
    elif index.upper() == 'NDWI':
        nd = img.normalizedDifference(['B3', 'B8']).rename('NDWI')
        vis = {'min': -0.5, 'max': 0.8, 'palette': ['#ffffff','#a6cee3','#1f78b4','#08519c']}
        return nd, vis
    else:
        # RGB visualize (convert to 8-bit visualization)
        vis = {'bands': ['B4','B3','B2'], 'min': 0, 'max': 3000}
        rgb = img.visualize(**vis)
        return ee.Image(rgb), {'min':0,'max':3000}

@app.route('/gee/map')
@require_api_key
def gee_map():
    start = request.args.get('start')
    end = request.args.get('end')
    index = request.args.get('index', 'NDVI')

    # defaults if missing: last 14 days
    if not start or not end:
        end_dt = datetime.utcnow().date()
        start_dt = end_dt - timedelta(days=14)
        start = start or start_dt.isoformat()
        end = end or end_dt.isoformat()

    key = f"{start}|{end}|{index}"
    cached = cache_get(key)
    if cached:
        return jsonify({'tileUrlTemplate': cached['tileUrl'], 'mapid': cached['mapid'], 'cached': True})

    try:
        img, vis = build_visual_image(start, end, index=index)
        mapid_dict = ee.Image(img).getMapId(vis)
        mapid = mapid_dict.get('mapid')
        token = mapid_dict.get('token')
        tile_url = f"https://earthengine.googleapis.com/map/{mapid}/{{z}}/{{x}}/{{y}}?token={token}"
        cache_set(key, {'mapid': mapid, 'token': token, 'tileUrl': tile_url})
        return jsonify({'tileUrlTemplate': tile_url, 'mapid': mapid, 'cached': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/gee/timeseries')
@require_api_key
def gee_timeseries():
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
        start = request.args.get('start')
        end = request.args.get('end')
        index = request.args.get('index', 'NDVI')

        if not (start and end):
            end_dt = datetime.utcnow().date()
            start_dt = end_dt - timedelta(days=90)
            start = start or start_dt.isoformat()
            end = end or end_dt.isoformat()

        start_date = ee.Date(start)
        end_date = ee.Date(end)
        point = ee.Geometry.Point(lon, lat)

        months = ee.List.sequence(0, end_date.difference(start_date, 'month').toInt())
        def month_map(n):
            n = ee.Number(n)
            s = start_date.advance(n, 'month')
            e = s.advance(1, 'month')
            col = (ee.ImageCollection('COPERNICUS/S2_SR')
                   .filterDate(s, e)
                   .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60)))
            img = col.median()
            if index.upper() == 'NDVI':
                idx = img.normalizedDifference(['B8', 'B4']).rename('NDVI'); band = 'NDVI'
            elif index.upper() == 'NDWI':
                idx = img.normalizedDifference(['B3', 'B8']).rename('NDWI'); band = 'NDWI'
            else:
                idx = img.select('B4').rename('B4'); band = 'B4'
            meanVal = idx.reduceRegion(ee.Reducer.mean(), point, 30).get(band)
            return ee.Feature(None, {'date': s.format('YYYY-MM-dd'), 'value': meanVal})

        feats = ee.FeatureCollection(months.map(month_map))
        dates = feats.aggregate_array('date').getInfo()
        values = feats.aggregate_array('value').getInfo()
        series = [{'date': d, 'value': v if v is not None else None} for d, v in zip(dates, values)]
        return jsonify({'series': series})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)
