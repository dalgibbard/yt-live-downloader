#!/usr/bin/env python3
import os
import subprocess
import json
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_basicauth import BasicAuth

app = Flask(__name__)

# Enable simple BasicAuth by setting envvars
username = os.environ.get("USERNAME", None)
password = os.environ.get("PASSWORD", None)
if username and password:
    app.config['BASIC_AUTH_USERNAME'] = username
    app.config['BASIC_AUTH_PASSWORD'] = password
    basic_auth = BasicAuth(app)
    app.config['BASIC_AUTH_FORCE'] = True

DOWNLOAD_FOLDER = 'downloads'
METADATA_FILE = 'downloads_metadata.json'
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER

downloads_metadata = []
download_services = {}

def load_downloads_metadata():
    metadata_path = os.path.join(app.config['DOWNLOAD_FOLDER'], METADATA_FILE)
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as metadata_file:
            return json.load(metadata_file)
    return []

def save_downloads_metadata():
    metadata_path = os.path.join(app.config['DOWNLOAD_FOLDER'], METADATA_FILE)
    with open(metadata_path, 'w') as metadata_file:
        json.dump(downloads_metadata, metadata_file, indent=4)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        youtube_url = request.form['youtube_url']
        download_id = len(downloads_metadata)
        download_path = os.path.join(app.config['DOWNLOAD_FOLDER'], f'download_{download_id}.mp4')
        status_file = f'status_{download_id}.json'
        command = f'python3 downloader.py {youtube_url} {download_path} {status_file}'
        process = subprocess.Popen(command, shell=True)
        downloads_metadata.append({"id": download_id, "url": youtube_url, "status": "In Progress", "downloaded_bytes": 0})
        # Store status file path for later use
        downloads_metadata[download_id]['status_file'] = status_file
        download_services[download_id] = {"process": process}
        save_downloads_metadata()
        return redirect(url_for('index'))  # Redirect after handling POST
    return render_template('index.html', downloads_metadata=downloads_metadata)

@app.route('/get_status', methods=['GET'])
def get_status():
    status_info = []
    for download in downloads_metadata:
        status_file = download.get('status_file')
        if status_file and os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
            download.update(status)
        status_info.append(download)
    return jsonify(status_info)


@app.route('/delete_download/<int:download_id>', methods=['POST'])
def delete_download(download_id):
    if download_id in download_services:
        download_services[download_id]['process'].terminate()
        del download_services[download_id]

    if download_id < len(downloads_metadata):
        download_metadata = downloads_metadata[download_id]
        if 'delete_files' in request.form and 'download_path' in download_metadata:
            try:
                os.remove(download_metadata['download_path'])
            except OSError as e:
                print(f"Error deleting file: {e}")
        downloads_metadata.pop(download_id)
        save_downloads_metadata()

    return redirect(url_for('index'))

if __name__ == '__main__':
    downloads_metadata = load_downloads_metadata()
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)
    app.run(host="0.0.0.0", port=5000, debug=True)
