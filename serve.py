import http.server
import os
import base64
import urllib.request
import urllib.error
import json
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get('PORT', 8080))
USERNAME = os.environ.get('DASHBOARD_USER', 'tao')
PASSWORD = os.environ.get('DASHBOARD_PASS', 'bittensor')
TAOSTATS_KEY = os.environ.get('TAOSTATS_API_KEY', '')
SCORE_INGEST_TOKEN = os.environ.get('SCORE_INGEST_TOKEN', '')
COINGECKO_KEY = os.environ.get('COINGECKO_API_KEY', '')

# In-memory cache of the cron's last v4 scoring JSON (Railway has no shared disk).
# Filled by POST /api/ingest-score, served by GET /api/score.
LATEST_SCORE = None

# In-memory cache of the cron's last computed cost-basis JSON.
# Filled by POST /api/ingest-cost-basis, served by GET /api/cost-basis.
LATEST_COST_BASIS = None

# In-memory cache of the last successful CoinGecko price response (bytes).
# Lets proxy_price serve a slightly-stale price on a transient upstream blip
# instead of a 502 — a rate-limit never reaches the browser after first success.
LATEST_PRICE_BODY = None

os.chdir(os.path.dirname(os.path.abspath(__file__)))

class AuthHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get('Authorization')
        if not auth:
            self.send_auth_request()
            return
        try:
            credentials = base64.b64decode(auth.split(' ')[1]).decode()
            user, pwd = credentials.split(':', 1)
            if user == USERNAME and pwd == PASSWORD:
                if self.path == '/' or self.path == '' or self.path == '/gordie' or self.path == '/gordie.html':
                    self.path = '/gordie.html'
                if self.path == '/legacy' or self.path == '/index.html':
                    self.path = '/index.html'
                if self.path == '/api/price':
                    return self.proxy_price()
                if self.path.startswith('/api/gordie/pools'):
                    return self.proxy_gordie_pools()
                if self.path.startswith('/api/portfolio/stakes'):
                    return self.proxy_portfolio_stakes()
                if self.path.startswith('/api/vtrust'):
                    return self.proxy_vtrust()
                if self.path.startswith('/api/yield'):
                    return self.proxy_yield()
                if self.path.startswith('/api/score'):
                    return self.serve_score()
                if self.path.startswith('/api/cost-basis'):
                    return self.serve_cost_basis()
                return super().do_GET()
        except:
            pass
        self.send_auth_request()

    def do_POST(self):
        path = self.path.rstrip('/')
        if path not in ('/api/ingest-score', '/api/ingest-cost-basis'):
            self.send_response(404)
            self.end_headers()
            return
        token = self.headers.get('X-Ingest-Token', '')
        if not SCORE_INGEST_TOKEN or token != SCORE_INGEST_TOKEN:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'forbidden')
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            if path == '/api/ingest-cost-basis':
                global LATEST_COST_BASIS
                LATEST_COST_BASIS = body.decode('utf-8')
            else:
                global LATEST_SCORE
                LATEST_SCORE = body.decode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def serve_score(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if LATEST_SCORE:
            self.wfile.write(LATEST_SCORE.encode('utf-8'))
        else:
            self.wfile.write(json.dumps({
                'status': 'awaiting_first_scan',
                'message': 'No scoring run ingested yet.',
                'ranked': []
            }).encode())

    def serve_cost_basis(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if LATEST_COST_BASIS:
            self.wfile.write(LATEST_COST_BASIS.encode('utf-8'))
        else:
            self.wfile.write(json.dumps({
                'status': 'awaiting_first_scan',
                'message': 'No cost-basis run ingested yet.',
                'positions': {}
            }).encode())

    def proxy_price(self):
        global LATEST_PRICE_BODY
        try:
            url = 'https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd,gbp'
            headers = {'User-Agent': 'TAO-Monitor/1.0'}
            if COINGECKO_KEY:
                headers['x-cg-demo-api-key'] = COINGECKO_KEY
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            LATEST_PRICE_BODY = data  # remember last good
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            # Transient CoinGecko hiccup (rate-limit / timeout): serve the last
            # good price (200, flagged stale) so it never reaches the browser as
            # a failure. Only 502 on a cold cache (no success yet this process).
            if LATEST_PRICE_BODY is not None:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('X-Price-Cache', 'stale')
                self.end_headers()
                self.wfile.write(LATEST_PRICE_BODY)
            else:
                self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

    def proxy_portfolio_stakes(self):
        try:
            coldkey = '5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR'
            url = f'https://api.taostats.io/api/dtao/stake_balance/latest/v1?coldkey={coldkey}&limit=100'
            req = urllib.request.Request(url, headers={
                'Authorization': TAOSTATS_KEY,
                'User-Agent': 'TAO-Monitor/1.0'
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def proxy_vtrust(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            netuid = params.get('netuid', ['0'])[0]
            url = f'https://api.taostats.io/api/metagraph/latest/v1?netuid={netuid}&limit=200'
            req = urllib.request.Request(url, headers={
                'Authorization': TAOSTATS_KEY,
                'User-Agent': 'TAO-Monitor/1.0'
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def proxy_yield(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            netuid = params.get('netuid', ['0'])[0]
            url = f'https://api.taostats.io/api/dtao/validator/yield/latest/v1?netuid={netuid}&limit=200'
            print(f'Fetching yield: {url}', flush=True)
            req = urllib.request.Request(url, headers={
                'Authorization': TAOSTATS_KEY,
                'User-Agent': 'TAO-Monitor/1.0'
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def proxy_gordie_pools(self):
        try:
            url = 'https://api.taostats.io/api/dtao/pool/latest/v1?limit=256'
            req = urllib.request.Request(url, headers={
                'Authorization': TAOSTATS_KEY,
                'User-Agent': 'TAO-Gordie/1.0'
            })
            with urllib.request.urlopen(req, timeout=45) as r:
                data = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def send_auth_request(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="TAO Monitor"')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'Unauthorized')

    def log_message(self, format, *args):
        pass

print(f"# v3 Serving on port {PORT}")
http.server.HTTPServer(('0.0.0.0', PORT), AuthHandler).serve_forever()
