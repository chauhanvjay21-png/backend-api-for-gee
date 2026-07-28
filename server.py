# server.py
import os
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort
from functools import lru_cache
import ee

# --------- Configuration (set these as environment variables) ----------
EE_SERVICE_ACCOUNT = os.environ.get('EE_SERVICE_ACCOUNT')  # e.g. river-auth-gee@...iam.gserviceaccount.com
EE_KEY_FILE = os.environ.get('EE_KEY_FILE')                # path inside container to JSON key
EE_PROJECT = os.environ.get('EE_PROJECT')                  # GCP project id
API_KEY = os.environ.get('API_KEY')                        # simple API key to protect endpoints
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', '*')   # configure properly in prod
# ---------------------------------------------------------------------

if not EE_SERVICE_ACCOUNT or not EE_KEY_FILE:
    raise RuntimeError('Set EE_SERVICE_ACCOUNT and EE_KEY_FILE environment variables')

# Initialize Earth Engine using service account
credentials = ee.ServiceAccountCredentials(EE_SERVICE_ACCOUNT, EE_KEY_FILE)
ee.Initialize(credentials, project=EE_PROJECT)

app = Flask(__name__)

# Simple API-key check (for demo). Replace / improve with real auth in production.
def require_api_key():
    key = request.headers.get('x-api-key') or request.args.get('api_key')
    if API_KEY and key != API_KEY:
        abort(401)

# Minimal CORS (for demo)
@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = ALLOWED_ORIGINS
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, x-api-key'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return resp

# Build index image and visualization
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
        rgb = img.visualize(bands=['B4','B3','B2'], min=0, max=3000)
        # For getMapId, ensure the image is an ee.Image
        return ee.Image(rgb), {'min':0, 'max':3000}

# Simple in-memory cache for mapid results keyed by (start,end,index)
# Value: {mapid, token, created_at}
CACHE = {}

def cache_get(key):
    entry = CACHE.get(key)
    if not entry: return None
    # token validity: map tokens generally valid for a few hours. Refresh after 2 hours.
    if time.time() - entry['created_at'] > 7200:
        del CACHE[key]
        return None
    return entry

def cache_set(key, value):
    CACHE[key] = value

@app.route('/gee/map')
def gee_map():
    require_api_key()
    start = request.args.get('start')
    end = request.args.get('end')
    index = request.args.get('index', 'NDVI')

    # sanity defaults
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
        # getMapId (note: ee.Image.getMapId expects visualization dict for non-visualized images)
        mapid_dict = ee.Image(img).getMapId(vis)
        mapid = mapid_dict['mapid']
        token = mapid_dict.get('token')
        tile_url = f"https://earthengine.googleapis.com/map/{mapid}/{{z}}/{{x}}/{{y}}?token={token}"
        cache_set(key, {'mapid': mapid, 'token': token, 'tileUrl': tile_url, 'created_at': time.time()})
        return jsonify({'tileUrlTemplate': tile_url, 'mapid': mapid, 'cached': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/gee/timeseries')
def gee_timeseries():
    require_api_key()
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

        # compute monthly steps
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
                idx = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
                band = 'NDVI'
            elif index.upper() == 'NDWI':
                idx = img.normalizedDifference(['B3', 'B8']).rename('NDWI')
                band = 'NDWI'
            else:
                idx = img.select('B4').rename('B4')
                band = 'B4'
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
