from flask import Flask, send_from_directory, abort, jsonify
import os
import json

app = Flask(__name__)
PWD = os.getcwd()
TILE_DIR = os.path.join(PWD, "tiles")
STATIC_DIR = os.path.join(PWD, "static")
NODES_FILE = os.path.join(PWD, "nodes.json")

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def serve_tile(z, x, y):
    tile_path = os.path.join(TILE_DIR, str(z), str(x), f"{y}.png")

    print("Serving tile:", tile_path)
    if os.path.exists(tile_path):
        return send_from_directory(os.path.dirname(tile_path), os.path.basename(tile_path))
    else:
        abort(404)

@app.route("/points")
def points():
    if not os.path.exists(NODES_FILE):
        return jsonify([]) # for testing - return empty list to client if nodes.json missing
        #abort(404) # uncomment after testing, to return 404 if file missing 
    with open(NODES_FILE) as f:
        data = json.load(f)
    return jsonify(data)



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)