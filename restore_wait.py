import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/healthz' else 404)
        self.end_headers()

HTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8000'))), HealthHandler).serve_forever()
