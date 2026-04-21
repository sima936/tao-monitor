from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8080))), SimpleHTTPRequestHandler).serve_forever()
