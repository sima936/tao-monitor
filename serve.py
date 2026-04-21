import http.server
import os
import base64

PORT = int(os.environ.get('PORT', 8080))
USERNAME = os.environ.get('DASHBOARD_USER', 'tao')
PASSWORD = os.environ.get('DASHBOARD_PASS', 'bittensor')

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
                return super().do_GET()
        except:
            pass
        self.send_auth_request()

    def send_auth_request(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="TAO Monitor"')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'Unauthorized')

print(f"Serving on port {PORT}")
http.server.HTTPServer(('0.0.0.0', PORT), AuthHandler).serve_forever()
