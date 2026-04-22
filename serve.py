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
TAOSTATS_KEY = 'tao-07fa8ae2-9d1d-4d70-8e91-7bb056604211:be6002dd'

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
                if self.path == '/' or self.path == '':
                    self.path = '/index.html'
                if self.path == '/api/price':
                    return self.proxy_price()
                if self.path.startswith('/api/vtrust'):
                    return self.proxy_vtrust()
                if self.path.startswith('/api/yield'):
                    return self.proxy_yield()
                return super().do_GET()
        except:
            pass
        self.send_auth_request()

    def proxy_price(self):
        try:
            url = 'https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd,gbp'
            req = urllib.request.Request(url, headers={'User-Agent': 'TAO-Monitor/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
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
            with urllib.request.urlopen(req, timeout=10) as r:
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
            req = urllib.request.Request(url, headers={
                'Authorization': TAOSTATS_KEY,
                'User-Agent': 'TAO-Monitor/1.0'
            })
            with urllib.request.urlopen(req, timeout=10) as r:
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

print(f"# v2Serving on port {PORT}")
http.server.HTTPServer(('0.0.0.0', PORT), AuthHandler).serve_forever()
