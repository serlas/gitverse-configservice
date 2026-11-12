import argparse
import os
import subprocess
import threading
import http.server
import socketserver
import json
import time
import datetime
import shutil

class ConfigService:
    def __init__(self, repo_path, port=8080, peers=None):
        self.repo_path = repo_path
        self.port = port
        self.peers = peers or []
        self.running = False
        self.init_repo()
        self.watch_thread = None
        self.sync_thread = None

    def init_repo(self):
        if not os.path.exists(self.repo_path):
            os.makedirs(self.repo_path)
        os.chdir(self.repo_path)
        try:
            subprocess.run(["git", "init"], check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "main"], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Failed to init repo: {e}")
            raise

    def set_value(self, key, value):
        key_path = os.path.join(self.repo_path, key.lstrip('/').replace('/', os.sep))
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write(value)
        os.chdir(self.repo_path)
        try:
            subprocess.run(["git", "add", key.lstrip('/')], check=True, capture_output=True)
            msg = f"set {key} at {datetime.datetime.utcnow().isoformat()}Z"
            subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Commit failed: {e}")

    def get_value(self, key):
        key_path = os.path.join(self.repo_path, key.lstrip('/').replace('/', os.sep))
        if os.path.exists(key_path):
            with open(key_path, 'r') as f:
                return f.read()
        return None

    def delete_value(self, key):
        key_path = os.path.join(self.repo_path, key.lstrip('/').replace('/', os.sep))
        if os.path.exists(key_path):
            os.remove(key_path)
            os.chdir(self.repo_path)
            try:
                subprocess.run(["git", "rm", key.lstrip('/')], check=True, capture_output=True)
                msg = f"deleted {key} at {datetime.datetime.utcnow().isoformat()}Z"
                subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                print(f"Delete commit failed: {e}")

    def sync_with_peers(self):
        while self.running:
            for peer in self.peers:
                try:
                    os.chdir(self.repo_path)
                    subprocess.run(["git", "pull", peer], check=True, capture_output=True)
                    subprocess.run(["git", "push", peer], check=True, capture_output=True)
                except subprocess.CalledProcessError as e:
                    print(f"Sync with {peer} failed: {e}")
            time.sleep(10)

    def watch_changes(self):
        last_commit = None
        while self.running:
            os.chdir(self.repo_path)
            try:
                result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
                current_commit = result.stdout.strip()
                if last_commit and last_commit != current_commit:
                    print(f"Change detected at {current_commit}")
                last_commit = current_commit
            except subprocess.CalledProcessError:
                pass
            time.sleep(1)

    def start_server(self):
        class ConfigHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                key = self.path[1:]
                value = self.server.config_service.get_value(key)
                self.send_response(200 if value else 404)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write((value or "Not found").encode())

            def do_POST(self):
                content_length = int(self.headers['Content-Length'])
                value = self.rfile.read(content_length).decode()
                key = self.path[1:]
                self.server.config_service.set_value(key, value)
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")

            def do_DELETE(self):
                key = self.path[1:]
                self.server.config_service.delete_value(key)
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")

        with socketserver.TCPServer(("", self.port), ConfigHandler) as httpd:
            httpd.config_service = self
            print(f"Serving at port {self.port}")
            self.running = True
            self.watch_thread = threading.Thread(target=self.watch_changes)
            self.watch_thread.start()
            if self.peers:
                self.sync_thread = threading.Thread(target=self.sync_with_peers)
                self.sync_thread.start()
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                self.running = False
                self.watch_thread.join()
                if self.sync_thread:
                    self.sync_thread.join()

def ttl_cleanup(config_service):
    while config_service.running:
        for root, _, files in os.walk(config_service.repo_path):
            for f in files:
                path = os.path.join(root, f)
                if os.path.exists(f"{path}.ttl"):
                    with open(f"{path}.ttl", 'r') as ttl_file:
                        ttl = int(ttl_file.read())
                    if time.time() - os.path.getmtime(path) > ttl:
                        config_service.delete_value(os.path.relpath(path, config_service.repo_path))
        time.sleep(5)

def cas_set(config_service, key, value, expected):
    current = config_service.get_value(key)
    if current == expected:
        config_service.set_value(key, value)
        return True
    return False

def vector_clock_update(config_service, node_id):
    vc_path = os.path.join(config_service.repo_path, ".vector_clock")
    vc = {"node_" + node_id: 0} if not os.path.exists(vc_path) else json.load(open(vc_path, 'r'))
    vc["node_" + node_id] += 1
    with open(vc_path, 'w') as f:
        json.dump(vc, f)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distributed Config Service")
    parser.add_argument("--repo", required=True, help="Path to Git repo")
    parser.add_argument("--port", type=int, default=8080, help="HTTP server port")
    parser.add_argument("--peers", nargs="*", help="List of peer repo URLs")
    args = parser.parse_args()

    service = ConfigService(args.repo, args.port, args.peers)
    ttl_thread = threading.Thread(target=ttl_cleanup, args=(service,))
    ttl_thread.start()
    service.start_server()