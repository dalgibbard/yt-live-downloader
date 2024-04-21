#!/usr/bin/env python3
import os
import json
import subprocess
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_basicauth import BasicAuth
from downloader import get_video_title

app = Flask(__name__)

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
def sanitize_filename(filename):
    return "".join([c if c.isalnum() else "_" for c in filename])

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        youtube_url = request.form['youtube_url']
        video_title = get_video_title(youtube_url)
        if not video_title:
            return "Failed to retrieve video title", 400

        sanitized_title = sanitize_filename(video_title)
        download_path = os.path.join(app.config['DOWNLOAD_FOLDER'], f'{sanitized_title}.mp4')
        status_file = os.path.join(app.config['DOWNLOAD_FOLDER'], f'status_{sanitized_title}.json')
        command = f'python3 downloader.py "{youtube_url}" "{download_path}" "{status_file}"'

        process = subprocess.Popen(command, shell=True)
        downloads_metadata.append({
            "title": video_title,
            "url": youtube_url,
            "status": "In Progress",
            "downloaded_bytes": 0,
            "download_path": download_path,
            "status_file": status_file
        })
        download_services[sanitized_title] = {"process": process}
        save_downloads_metadata()
        return redirect(url_for('index'))  # Redirect after POST
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

@app.route('/delete_download/<string:sanitized_title>', methods=['POST'])
def delete_download(sanitized_title):
    download_metadata = next((d for d in downloads_metadata if sanitize_filename(d['title']) == sanitized_title), None)
    if download_metadata:
        process = download_services.get(sanitized_title, {}).get('process')
        if process:
            process.terminate()
            del download_services[sanitized_title]

        if 'delete_files' in request.form:
            try:
                os.remove(download_metadata['download_path'])
            except OSError as e:
                print(f"Error deleting file: {e}")

        downloads_metadata.remove(download_metadata)
        save_downloads_metadata()

    return redirect(url_for('index'))

if __name__ == '__main__':
    downloads_metadata = load_downloads_metadata()
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)
    app.run(host="0.0.0.0", port=5000, debug=True)
