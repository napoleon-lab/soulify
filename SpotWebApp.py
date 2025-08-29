from flask import Flask, redirect, request, session, url_for, render_template, jsonify, Response, stream_with_context
from CommandConstruct import (construct_track_download_command,
                              construct_album_download_command,
                              construct_artist_download_command,
                              construct_playlist_download_command)
from config_manager import get_config, get_setting, write_config, generate_sldl_config
from datetime import datetime
import requests
import base64
import urllib.parse
import os
import subprocess
import logging
import time
import sys
import uuid
import shlex
from threading import Thread
import re
import threading
import shutil
import mutagen
import queue
import pexpect
import json
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON, TPE1, TALB
from mutagen.mp4 import MP4
from mutagen.wavpack import WavPack
import stat
from collections import deque

from werkzeug import serving

parent_log_request = serving.WSGIRequestHandler.log_request


def log_request(self, *args, **kwargs):
    if any(self.path.startswith(p) for p in ['/update_console_output', '/health', '/ping']):
        return

    parent_log_request(self, *args, **kwargs)


serving.WSGIRequestHandler.log_request = log_request

logging.basicConfig(
    level=logging.INFO,  # Use INFO or DEBUG
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Logs go to stdout (visible in Docker)
    ]
)

# Store information about active downloads
active_downloads = {}
command_lock = threading.Lock() 

command_processes = {}
command_statuses = {}
command_output_queues = {}

# Define a queue for pending download commands
download_queue = deque()

# Define a thread-safe queue handler thread
queue_handler_thread = None

# Define active_commands dictionary here
active_commands = {}

download_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = 'your_secret_key'


command_process = None

# Path to configuration and post-processing scripts
base_dir = os.path.dirname(os.path.abspath(__file__))
postdownload_scripts_dir = os.path.join(base_dir, 'scripts', 'postdownload')
run_all_script = os.path.join(postdownload_scripts_dir, 'RunAll.py')
sort_move_music_script = os.path.join(postdownload_scripts_dir, 'Sort_MoveMusicDownloads.py')
update_with_mb_script = os.path.join(postdownload_scripts_dir, 'UpdatewithMB.sh')

ansi_escape = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')

def clean_special_chars(query):
    # First remove all commas
    query = query.replace(',', '')
    # Then remove all other special characters except for alphanumeric, spaces, and hyphens
    return re.sub(r'[^a-zA-Z0-9\s\-]', '', query)


def log_post_processing_output(result, script_name):
    """Logs the output of post-processing scripts."""
    logging.info(f"--- {script_name} stdout ---")
    print(result.stdout)
    logging.info(f"--- {script_name} stderr ---")
    if result.stderr:
        print(result.stderr)
    logging.info(f"--- End of {script_name} output ---")


# Function to execute post-processing scripts based on the configuration
def run_post_processing(command_id):
    """
    Runs post-processing scripts and, if the download was a playlist,
    triggers Jellyfin playlist creation.
    """
    update_with_mb = get_setting('PostProcessing', 'update_metadata_with_musicbrainz', 'True').lower() == 'true'
    update_library = get_setting('PostProcessing', 'update_library_metadata_and_refresh_jellyfin', 'True').lower() == 'true'

    # Check conditions and run the appropriate scripts
    if update_library and update_with_mb:
        logging.info(f"[{command_id}] Running RunAll.py script...")
        result = subprocess.run(['python3', run_all_script], capture_output=True, text=True)
        log_post_processing_output(result, 'RunAll.py')
    elif update_library and not update_with_mb:
        logging.info(f"[{command_id}] Running Sort_MoveMusicDownloads.py script...")
        result = subprocess.run(['python3', sort_move_music_script], capture_output=True, text=True)
        log_post_processing_output(result, 'Sort_MoveMusicDownloads.py')
    elif update_with_mb and not update_library:
        logging.info(f"[{command_id}] Running UpdatewithMB.sh script...")
        result = subprocess.run(['bash', update_with_mb_script], capture_output=True, text=True)
        log_post_processing_output(result, 'UpdatewithMB.sh')
    else:
        logging.info(f"[{command_id}] No post-processing scripts to run.")

    # After sorting, create Jellyfin playlist if it was a playlist download
    with command_lock:
        command_info = active_downloads.get(command_id, {})
    
# Dynamically construct the paths based on the location of SpotWebApp.py
base_dir = os.path.dirname(os.path.abspath(__file__))
sldlPath = os.path.join(base_dir, 'sldl')
sldlConfigPath = os.path.join(base_dir, 'sldl.conf')

# Load Spotify settings from config_manager
CLIENT_ID = get_setting('Spotify', 'client_id')
CLIENT_SECRET = get_setting('Spotify', 'client_secret')
REDIRECT_URI = get_setting('Spotify', 'redirect_uri')


# Spotify API URLs and scope
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPE = "user-read-private user-read-email playlist-read-private playlist-read-collaborative"

# Ensure valid token
def ensure_valid_token():
    """Ensure that the Spotify access token is valid or refresh if needed."""
    access_token = session.get('access_token')
    if not access_token:
        return redirect(url_for('login'))

    response = requests.get('https://api.spotify.com/v1/me', headers={'Authorization': f'Bearer {access_token}'})
    if response.status_code == 401:  # Token has expired
        if refresh_spotify_token():
            access_token = session.get('access_token')
        else:
            return redirect(url_for('login'))
    return access_token

def refresh_spotify_token():
    """Utility to refresh the Spotify access token."""
    refresh_token = session.get('refresh_token')
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data)
    if response.status_code == 200:
        token_data = response.json()
        session['access_token'] = token_data['access_token']
        session['refresh_token'] = token_data.get('refresh_token', refresh_token)  # Keep old refresh token if none is provided
        return True
    return False  # If refresh fails

def get_client_credentials_token():
    """Gets a token using client credentials flow."""
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {'grant_type': 'client_credentials'}
    response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data)
    if response.status_code == 200:
        return response.json()['access_token']
    return None

def get_token():
    """Gets a user token if available, otherwise gets a client token."""
    if 'access_token' in session:
        return ensure_valid_token()
    return get_client_credentials_token()


@app.route('/')
def index():
    """Home route."""
    access_token = ensure_valid_token()  # Check for valid token and refresh if needed
    if not access_token:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/playlists')
def playlists():
    """List the user's Spotify playlists or search for playlists."""
    access_token = get_token()
    if not access_token:
        return "Could not authenticate with Spotify.", 401

    headers = {'Authorization': f'Bearer {access_token}'}
    search_query = request.args.get('search_query')
    playlists = []

    if search_query:
        url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(search_query)}&type=playlist&limit=50"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            playlists = data.get('playlists', {}).get('items', [])
        else:
            return f"Error fetching playlists: {response.json()}"
    elif 'access_token' in session:
        url = "https://api.spotify.com/v1/me/playlists?limit=50&offset=0"
        while url:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                playlists.extend(data['items'])
                url = data['next']
            else:
                return f"Error fetching user playlists: {response.json()}"

    return render_template('playlist.html', playlists=playlists)

@app.route('/playlisttracks/<playlist_id>')
def playlist_tracks(playlist_id):
    """View tracks from a specific playlist."""
    access_token = ensure_valid_token()
    if not access_token:
        return redirect(url_for('login'))

    playlist_name = request.args.get('playlist_name')
    headers = {'Authorization': f'Bearer {access_token}'}
    url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100'
    tracks = []
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            tracks.extend(data['items'])
            url = data['next']
        else:
            return f"Error fetching playlist tracks: {response.json()}"

    return render_template('playlisttracks.html', tracks=tracks, playlist_name=playlist_name)

@app.route('/albumtracks/<album_id>')
def album_tracks(album_id):
    """View tracks from a specific album."""
    access_token = ensure_valid_token()  # Ensure we have a valid token
    if not access_token:
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {access_token}'}
    album_url = f'https://api.spotify.com/v1/albums/{album_id}'
    tracks_url = f'https://api.spotify.com/v1/albums/{album_id}/tracks?limit=50'
    
    # Fetching album details (name, images, etc.)
    album_response = requests.get(album_url, headers=headers)
    if album_response.status_code != 200:
        return f"Error fetching album details: {album_response.json()}"
    album_data = album_response.json()
    
    # Fetching album tracks
    tracks_response = requests.get(tracks_url, headers=headers)
    if tracks_response.status_code != 200:
        return f"Error fetching album tracks: {tracks_response.json()}"
    tracks_data = tracks_response.json()

    # Group tracks by disc number
    grouped_tracks = {}
    for track in tracks_data['items']:
        disc_number = track.get('disc_number', 1)
        if disc_number not in grouped_tracks:
            grouped_tracks[disc_number] = []
        grouped_tracks[disc_number].append(track)

    return render_template('albumtracks.html', album=album_data, grouped_tracks=grouped_tracks)




@app.route('/artist/<artist_id>')
def artist_albums(artist_id):
    """View albums from a specific artist and related artists."""
    access_token = ensure_valid_token()  # Ensure we have a valid token
    if not access_token:
        return redirect(url_for('login'))

    # Get artist details (name, etc.)
    artist_url = f'https://api.spotify.com/v1/artists/{artist_id}'
    headers = {'Authorization': f'Bearer {access_token}'}
    artist_response = requests.get(artist_url, headers=headers)
    
    if artist_response.status_code == 200:
        artist = artist_response.json()  # This will contain the artist's name, genres, etc.
    else:
        return f"Error fetching artist details: {artist_response.json()}"

    # Get artist albums
    albums_url = f'https://api.spotify.com/v1/artists/{artist_id}/albums?limit=50&include_groups=album,single,compilation,appears_on'
    albums_response = requests.get(albums_url, headers=headers)
    
    if albums_response.status_code == 200:
        albums = albums_response.json()['items']
    else:
        return f"Error fetching artist albums: {albums_response.json()}"

    # Group albums by album_group
    grouped_albums = {
        "album": [],
        "single": [],
        "compilation": [],
        "appears_on": []
    }
    for album in albums:
        album_group = album.get('album_group', 'album')  # Use 'album_group' instead of 'album_type'
        grouped_albums[album_group].append(album)

    # Get related artists
    related_artists_url = f'https://api.spotify.com/v1/artists/{artist_id}/related-artists'
    related_artists_response = requests.get(related_artists_url, headers=headers)

    if related_artists_response.status_code == 200:
        related_artists = related_artists_response.json()['artists']
    else:
        related_artists = []

    # Pass the artist details along with albums grouped by type and related artists to the template
    return render_template(
        'artistalbums.html', 
        artist=artist,           
        albums=grouped_albums,  # Albums grouped by album_group
        related_artists=related_artists
    )




@app.route('/search', methods=['GET', 'POST'])
def search():
    """Search for artists, albums, or tracks."""
    if request.method == 'POST':
        query = request.form.get('search_query')
        search_type = request.form.get('search_type')

        if not query or not search_type:
            return render_template('search.html', error="Please enter a search query and select a search type.")

        access_token = ensure_valid_token()
        if not access_token:
            return redirect(url_for('login'))

        headers = {'Authorization': f'Bearer {access_token}'}
        search_url = f'https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type={search_type}'
        response = requests.get(search_url, headers=headers)

        if response.status_code == 200:
            search_results = response.json()
            return render_template('search_results.html', search_results=search_results, search_type=search_type)
        else:
            return f"Error fetching search results: {response.json()}"

    return render_template('search.html')


@app.route('/login')
def login():
    """Spotify login route."""
    auth_url = f"{SPOTIFY_AUTH_URL}?response_type=code&client_id={CLIENT_ID}&scope={urllib.parse.quote(SCOPE)}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    return redirect(auth_url)

@app.route('/logout')
def logout():
    """Logout route."""
    session.clear()  # Clear session tokens
    return redirect(url_for('login'))

@app.route('/callback')
def callback():
    """Spotify authorization callback."""
    code = request.args.get('code')
    if not code:
        return "Authorization failed."

    token_response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        },
        headers={
            'Authorization': 'Basic ' + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    )

    if token_response.status_code != 200:
        return f"Error fetching access token: {token_response.json()}"

    token_data = token_response.json()
    session['access_token'] = token_data.get('access_token')
    session['refresh_token'] = token_data.get('refresh_token')

    return redirect(url_for('index'))

@app.route('/refresh_token')
def refresh_token():
    """Refresh the Spotify access token."""
    if refresh_spotify_token():
        return redirect(url_for('index'))
    else:
        return "Failed to refresh token, please log in again."




@app.route('/downloads')
def downloads():
    return render_template('downloads.html')

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config = get_config()

        # sldl/soulseek/spotify settings
        config.set('Soulseek', 'username', request.form.get('username'))
        config.set('Soulseek', 'password', request.form.get('password'))
        config.set('Paths', 'music_download_folder', request.form.get('path'))
        config.set('sLDL', 'no-remove-special-chars', request.form.get('no-remove-special-chars'))
        config.set('sLDL', 'pref-format', request.form.get('pref-format'))
        config.set('Spotify', 'client_id', request.form.get('spotify-id'))
        config.set('Spotify', 'client_secret', request.form.get('spotify-secret'))
        config.set('sLDL', 'm3u', request.form.get('m3u'))

        # soulify settings
        config.set('PostProcessing', 'update_metadata_with_musicbrainz', str(request.form.get('UpdatemetadataWithMusicBrainz') == 'true'))
        config.set('PostProcessing', 'update_library_metadata_and_refresh_jellyfin', str(request.form.get('UpdateLibraryMetadataAndRefreshJellyfin') == 'true'))

        # pdscript/jellyfin/paths settings
        config.set('Paths', 'destination_root', request.form.get('destination_root'))
        config.set('Paths', 'new_artists_dir', request.form.get('new_artists_dir'))
        config.set('Jellyfin', 'api_base_url', request.form.get('api_base_url'))
        config.set('Jellyfin', 'api_auth_token', request.form.get('api_auth_token'))

        write_config(config)
        generate_sldl_config()

        return redirect(url_for('settings'))

    # For GET request, display current settings
    config = get_config()
    sldl_settings = {
        'username': config.get('Soulseek', 'username', fallback=''),
        'password': config.get('Soulseek', 'password', fallback=''),
        'path': config.get('Paths', 'music_download_folder', fallback=''),
        'no-remove-special-chars': config.get('sLDL', 'no-remove-special-chars', fallback='false'),
        'pref-format': config.get('sLDL', 'pref-format', fallback=''),
        'spotify-id': config.get('Spotify', 'client_id', fallback=''),
        'spotify-secret': config.get('Spotify', 'client_secret', fallback=''),
        'm3u': config.get('sLDL', 'm3u', fallback='none')
    }
    soulify_settings = (
        config.getboolean('PostProcessing', 'update_metadata_with_musicbrainz', fallback=True),
        config.getboolean('PostProcessing', 'update_library_metadata_and_refresh_jellyfin', fallback=True)
    )
    pdscript_settings = {
        'destination_root': config.get('Paths', 'destination_root', fallback=''),
        'new_artists_dir': config.get('Paths', 'new_artists_dir', fallback=''),
        'api_base_url': config.get('Jellyfin', 'api_base_url', fallback=''),
        'api_auth_token': config.get('Jellyfin', 'api_auth_token', fallback='')
    }
    return render_template('settings.html', sldl=sldl_settings, soulify=soulify_settings, pdscript=pdscript_settings)

@app.route('/post_download_management')
def post_download_management():
    return render_template('post_download_management.html')

@app.route('/move_artist_folder', methods=['POST'])
def move_artist_folder():
    artist_name = request.form['artistName']
    genre = request.form['genre']
    new_artists_dir = get_setting('Paths', 'new_artists_dir')
    destination_root = get_setting('Paths', 'destination_root')

    source_path = os.path.join(new_artists_dir, artist_name)
    target_path = os.path.join(destination_root, genre, artist_name)

    try:
        # Set permissions to read/write for everyone
        for root, dirs, files in os.walk(source_path):
            for momo in dirs:
                os.chmod(os.path.join(root, momo), 0o777)
            for momo in files:
                os.chmod(os.path.join(root, momo), 0o666)

        # Move folder
        shutil.move(source_path, target_path)
        return jsonify({'success': True, 'message': f'Successfully moved {artist_name} to {genre}.'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to move {artist_name}: {str(e)}'})




@app.route('/ImportnewArtists')
def import_new_artists():
    try:
        # Retrieve paths from the configuration
        new_artists_dir = get_setting('Paths', 'new_artists_dir')
        destination_root = get_setting('Paths', 'destination_root')

        # List all folders at the root of the destination_root and sort them
        genres = [folder for folder in os.listdir(destination_root) if os.path.isdir(os.path.join(destination_root, folder))]
        genres.sort(key=lambda s: s.strip().upper())  # Strip whitespace and sort ignoring case

    except Exception as e:
        logging.error(f"Error reading config or directories: {e}")
        genres = []  # Fallback to an empty list if there's a configuration read error

    artist_folders = [
        folder for folder in os.listdir(new_artists_dir)
        if os.path.isdir(os.path.join(new_artists_dir, folder))
    ]

    # Pass the list of artist folders and genres to the template
    return render_template('import_new_artists.html', artist_folders=artist_folders, genres=genres)


def retrieve_files(folder_path):
    file_list = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_list.append(os.path.join(root, file))
    return file_list
    
def retrieve_file_details(folder_path):
    file_details = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            full_path = os.path.join(root, file)
            filename = os.path.basename(full_path)
            file_details.append((filename, full_path))
    return file_details


# Function to update the metadata of an audio file
def update_audio_metadata(file_path, artist, album_artist, genre, album, title, track_number):
    try:
        # Attempt to open the file with mutagen's general File method
        audio = mutagen.File(file_path, easy=True)
        
        if audio is None:
            print(f"File {file_path} is not a valid audio file or format not supported.")
            return

        # Handle MP3 files with EasyID3
        if file_path.lower().endswith(".mp3"):
            audio = EasyID3(file_path)
        # Handle FLAC files
        elif file_path.lower().endswith(".flac"):
            audio = FLAC(file_path)
        # Handle MP4/M4A files
        elif file_path.lower().endswith((".mp4", ".m4a")):
            audio = MP4(file_path)
            audio["\xa9ART"] = artist      # Artist
            audio["\xa9alb"] = album       # Album
            audio["\xa9nam"] = title       # Track title
            audio["\xa9gen"] = genre       # Genre
            audio["trkn"] = [(int(track_number), 0)]  # Track number
        # Handle WAV files
        elif file_path.lower().endswith(".wav"):
            print(f"Warning: WAV files have limited metadata support with Mutagen. File: {file_path}")
        # Handle other formats, such as WavPack
        elif file_path.lower().endswith(".wv"):
            audio = WavPack(file_path)

        # Update common tags for supported formats (excluding special formats like MP4, which was handled above)
        if audio and not isinstance(audio, MP4):
            audio['artist'] = artist
            audio['albumartist'] = album_artist
            audio['genre'] = genre
            audio['album'] = album
            audio['title'] = title
            audio['tracknumber'] = track_number  # Track number

        # Save the updated metadata
        audio.save()
        print(f"Updated metadata for {file_path}")

    except mutagen.MutagenError as e:
        print(f"Failed to update metadata for {file_path}: {e}")

# Endpoint to process metadata update for multiple files
@app.route('/process_metadata', methods=['POST'])
def process_metadata():
    # Get the unknown albums directory path from the configuration
    unknown_albums_dir = get_setting('Paths', 'unknown_albums_dir')

    # Get form data (submitted values)
    folder = request.form.get('folder')
    artist = request.form.get('artist')  # Corrected from 'album_name_adj'
    album_artist = request.form.get('album_artist')  # Corrected from 'album_name_adj'
    genre = request.form.get('genre')
    album = request.form.get('album')  # This corresponds to 'album_name_adj' in HTML

    # Construct the full path to the folder
    folder_path = os.path.join(unknown_albums_dir, folder)

    # Track any failed metadata updates
    failed_files = []

    # Go through each file in the folder and update the metadata if it's an audio file
    for root, dirs, files in os.walk(folder_path):
        for index, file in enumerate(files):
            file_path = os.path.join(root, file)

            # Only process known audio file types
            if file.lower().endswith(('.mp3', '.flac', '.aac', '.wav', '.m4a')):
                # Extract the track number and track name from the form data
                track_number = request.form.get(f'track_number_{index}', '1')  # Default track number to 1 if missing
                track_name = request.form.get(f'track_name_{index}', os.path.splitext(file)[0])  # Default to file name
                
                try:
                    update_audio_metadata(file_path, artist, album_artist, genre, album, track_name, track_number)
                except Exception as e:
                    failed_files.append(file_path)
                    print(f"Failed to update metadata for {file_path}: {e}")

    if failed_files:
        return jsonify({'message': 'Some files failed to update', 'failed_files': failed_files}), 500
    else:
        return jsonify({'message': 'Metadata updated successfully.'})


@app.route('/ImportUnknownAlbum', methods=['POST'])
def import_unknown_album():
    # Retrieve the unknown albums directory path from the configuration
    unknown_albums_dir = get_setting('Paths', 'unknown_albums_dir')

    # Get form data (submitted values)
    folder = request.form.get('folder')
    if not folder:
        return "Error: Folder not specified.", 400
    artist = request.form.get('artist')
    genre = request.form.get('genre')
    album_type = request.form.get('albumType')
    original_year = request.form.get('originalYear')
    release_year = request.form.get('releaseYear')
    album_name_adj = request.form.get('albumNameAdj')
    country = request.form.get('country')
    media_format = request.form.get('mediaFormat')
	# Safely convert totalDiscs to int
    try:
        total_discs = int(request.form.get('totalDiscs', '1'))  # Default to 1 if not provided
    except ValueError:
        return "Invalid input for total discs. Please enter a valid number.", 400

    # Handle "Other" option for country if selected
    if country == "Other":
        country = request.form.get('otherCountry')

    # Construct strings
    artist_folder = artist
    album_folder = f"[{album_type}] [{original_year}] {album_name_adj}"
    release_folder = f"[{media_format}] [{country}] [{release_year}]"
    discs = [f"{media_format} {str(i+1).zfill(2)}" for i in range(total_discs)]


    # Full path to the unknown album folder
    folder_path = os.path.join(unknown_albums_dir, folder)
    
    

    # Retrieve all files (filenames and full paths)
    file_details = retrieve_file_details(folder_path)
    
	# Pass the selected options and the file list to the template
    return render_template('import_unknown_album.html', 
                           folder=folder,
                           artist=artist,
                           genre=genre,
                           album_type=album_type,
                           original_year=original_year,
                           release_year=release_year,
                           album_name_adj=album_name_adj,
                           country=country,
                           media_format=media_format,
                           total_discs=total_discs,
                           artist_folder=artist_folder,
                           album_folder=album_folder,
                           release_folder=release_folder,
                           discs=discs,
                           file_details=file_details)

@app.route('/unknownAlbums')
def unknown_albums():
    # Retrieve paths from the configuration
    unknown_albums_dir = get_setting('Paths', 'unknown_albums_dir')
    destination_root = get_setting('Paths', 'destination_root')

    # Get the list of folders (root level only) for unknown albums
    unknown_album_folders = sorted([folder for folder in os.listdir(unknown_albums_dir)
                                    if os.path.isdir(os.path.join(unknown_albums_dir, folder))])

    # Get genre folders (folder depth 0) and sort them alphabetically
    genres = sorted([folder for folder in os.listdir(destination_root)
                     if os.path.isdir(os.path.join(destination_root, folder))])
    
    # Get artist folders (folder depth 1, inside each genre folder) and sort them alphabetically
    artist_folders = []
    for genre in genres:
        genre_path = os.path.join(destination_root, genre)
        artist_folders.extend([folder for folder in os.listdir(genre_path)
                               if os.path.isdir(os.path.join(genre_path, folder))])
    
    # Sort the artist folders alphabetically
    artist_folders = sorted(artist_folders)

    return render_template('unknown_albums.html', unknown_album_folders=unknown_album_folders, genres=genres, artist_folders=artist_folders)

@app.route('/rename_files', methods=['POST'])
def rename_files():
    data = request.json
    folder = data.get('folder', '')

    # Retrieve the unknown albums directory path from the configuration
    unknown_albums_dir = get_setting('Paths', 'unknown_albums_dir')
    
    # Construct the full path to the folder
    folder_path = os.path.join(unknown_albums_dir, folder)
    
    if not os.path.exists(folder_path):
        return jsonify({'error': 'Folder does not exist'}), 400

    # Ensure the folder has read/write permissions for everyone
    os.chmod(folder_path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)

    # Retrieve file details
    file_details = retrieve_file_details(folder_path)

    # Only allow audio files (e.g., .mp3, .flac, .aac, .wav)
    valid_audio_extensions = ['.mp3', '.flac', '.aac', '.wav', '.m4a']
    files_to_rename = {
        file[0]: file for file in file_details
        if os.path.splitext(file[0])[1].lower() in valid_audio_extensions
    }

    errors = []
    for file in data['files']:
        old_file_name = file['oldFileName']
        new_file_name = file['newFileName']

        if old_file_name in files_to_rename:
            old_file_path = files_to_rename[old_file_name][1]  # Get full path
            file_extension = os.path.splitext(old_file_path)[1]

            new_file_path = os.path.join(folder_path, new_file_name)

            # Ensure the file has read/write permissions for everyone before renaming
            os.chmod(old_file_path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)

            try:
                os.rename(old_file_path, new_file_path)
            except Exception as e:
                errors.append(f"Failed to rename {old_file_name} to {new_file_name}: {str(e)}")
        else:
            errors.append(f"File not found: {old_file_name}")

    if errors:
        return jsonify({'message': 'Some files failed to rename', 'errors': errors}), 500
    else:
        return jsonify({'message': 'Files renamed successfully'}), 200



def merge_directories(src, dst):
    """
    Recursively merge contents of src directory into dst directory.
    If a file in src already exists in dst, it will be overwritten.
    """
    if not os.path.exists(dst):
        # If destination doesn't exist, simply move the source to destination
        shutil.move(src, dst)
    else:
        # If destination exists, merge contents
        for item in os.listdir(src):
            src_item = os.path.join(src, item)
            dst_item = os.path.join(dst, item)
            
            if os.path.isdir(src_item):
                # If the item is a directory, recursively merge it
                merge_directories(src_item, dst_item)
            else:
                # If the item is a file, copy it to the destination
                shutil.copy2(src_item, dst_item)
        
        # If the source directory is empty after moving files, remove it
        if not os.listdir(src):
            os.rmdir(src)


@app.route('/organize_album', methods=['POST'])
def organize_album():
    try:
        data = request.json

        # Extract necessary information from the request
        folder = data.get('folder', '')
        artist_folder = data.get('artist_folder', '')
        album_folder = data.get('album_folder', '')
        media_format = data.get('media_format', '')
        release_folder = data.get('release_folder', '')
        genre = data.get('genre', '')  # Genre is already decoded

        # Configuration to access paths
        unknown_albums_dir = get_setting('Paths', 'unknown_albums_dir')
        destination_root = get_setting('Paths', 'destination_root')

        # Construct full paths using the values from the request
        full_genre_path = os.path.join(unknown_albums_dir, genre)
        full_artist_path = os.path.join(full_genre_path, artist_folder)
        full_album_path = os.path.join(full_artist_path, album_folder)
        full_release_path = os.path.join(full_album_path, release_folder)

        # Ensure the directory structure exists
        os.makedirs(full_release_path, exist_ok=True)

        # Process each file based on its disc number
        for file in data['files']:
            old_file_name = file['oldFileName']
            disc_number = file.get('discNumber')  # Disc number for each file (if any)
            old_file_path = os.path.join(unknown_albums_dir, folder, old_file_name)

            if not os.path.exists(old_file_path):
                continue  # Skip this file if it does not exist

            # Determine the new file path based on whether a disc number is present
            if disc_number:
                new_file_path = os.path.join(full_release_path, f"{media_format} {str(disc_number).zfill(2)}", old_file_name)
            else:
                new_file_path = os.path.join(full_release_path, old_file_name)

            # Ensure the target directory exists
            os.makedirs(os.path.dirname(new_file_path), exist_ok=True)
            shutil.move(old_file_path, new_file_path)

        # Now, merge the entire genre directory (after it's constructed) to the destination root
        merge_directories(full_genre_path, os.path.join(destination_root, genre))

        return jsonify({'message': 'Album organized successfully!'}), 200

    except Exception as e:
        return jsonify({'error': f"Exception during operation: {str(e)}"}), 500

@app.route('/browse')
def browse():
    """Browse Spotify categories."""
    access_token = ensure_valid_token()  # Ensure valid Spotify token
    if not access_token:
        return redirect(url_for('login'))

    # Spotify API base URL for categories
    url = 'https://api.spotify.com/v1/browse/categories?locale=en_GB&limit=50'
    headers = {'Authorization': f'Bearer {access_token}'}
    categories = []

    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            categories.extend(data['categories']['items'])
            url = data['categories']['next']  # Move to the next page if available
        else:
            return f"Error fetching categories: {response.json()}"

    return render_template('browse.html', categories=categories)

@app.route('/browse_details/<category_id>')
def browse_details(category_id):
    """Fetch and display playlists for a specific Spotify category."""
    access_token = ensure_valid_token()
    if not access_token:
        return redirect(url_for('login'))

    # Step 1: Fetch category details to get the category name
    category_url = f'https://api.spotify.com/v1/browse/categories/{category_id}'
    headers = {'Authorization': f'Bearer {access_token}'}
    category_response = requests.get(category_url, headers=headers)

    if category_response.status_code == 200:
        category_data = category_response.json()
        category_name = category_data.get('name', 'Unknown Category')
    else:
        return f"Error fetching category details: {category_response.json()}"

    # Step 2: Fetch playlists in the category
    playlists_url = f'https://api.spotify.com/v1/browse/categories/{category_id}/playlists'
    playlists_response = requests.get(playlists_url, headers=headers)

    if playlists_response.status_code == 200:
        data = playlists_response.json()
        category_playlists = data.get('playlists', {}).get('items', [])
    else:
        return f"Error fetching playlists: {playlists_response.json()}"

    # Step 3: Render the template and pass category name and playlists
    return render_template(
        'browse_details.html',
        category_playlists=category_playlists,
        category_name=category_name  # Pass category name to the template
    )



@app.route('/track_preview/<track_id>')
def track_preview(track_id):
    """Fetch track details and return the preview URL."""
    access_token = ensure_valid_token()
    if not access_token:
        return redirect(url_for('login'))

    # Spotify API URL for track details
    url = f'https://api.spotify.com/v1/tracks/{track_id}'
    headers = {'Authorization': f'Bearer {access_token}'}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        track_data = response.json()
        preview_url = track_data.get('preview_url')
        return jsonify({'preview_url': preview_url})
    else:
        return jsonify({'error': 'Could not fetch track details'}), 500
        
@app.route('/create_artist')
def create_artist():
    """Display the form to create a new artist with genre selection."""
    destination_root = get_setting('Paths', 'destination_root')
    
    # List genres (subdirectories inside destination_root)
    try:
        genres = [folder for folder in os.listdir(destination_root) if os.path.isdir(os.path.join(destination_root, folder))]
        genres.sort(key=lambda s: s.strip().upper())  # Sort genres alphabetically
    except Exception as e:
        genres = []
        logging.error(f"Error reading genres: {str(e)}")
    
    return render_template('create_artist.html', genres=genres)

@app.route('/submit_artist', methods=['POST'])
def submit_artist():
    """Create a new artist folder inside the selected genre."""
    artist_name = request.form.get('artist_name')
    genre = request.form.get('genre')

    # Ensure the artist name and genre are not blank
    if not artist_name or not genre:
        return jsonify({'error': 'Artist name and genre cannot be blank'}), 400

    # Read the configuration to get the destination_root path
    destination_root = get_setting('Paths', 'destination_root')

    # Sanitize the artist name to remove special characters
    safe_artist_name = clean_special_chars(artist_name)

    # Create the folder path inside the selected genre
    artist_folder_path = os.path.join(destination_root, genre, safe_artist_name)

    try:
        # Create the folder if it doesn't exist
        os.makedirs(artist_folder_path, exist_ok=True)
        return jsonify({'success': True, 'message': f'Artist "{safe_artist_name}" created successfully in genre "{genre}".'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scan_jellyfin_library', methods=['POST'])
def scan_jellyfin_library():
    api_base_url = get_setting('Jellyfin', 'api_base_url')
    main_music_library_id = get_setting('Jellyfin', 'main_music_library_id')
    api_auth_token = get_setting('Jellyfin', 'api_auth_token')
    
    url = f"{api_base_url}/Items/{main_music_library_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
    headers = {
        'Authorization': f"MediaBrowser Token={api_auth_token}",
        'Accept': 'application/json'
    }

    response = requests.post(url, headers=headers)
    if response.status_code == 204:
        return jsonify({'message': 'Jellyfin Library Scan Initiated Successfully'}), 204
    else:
        return jsonify({'message': f'Failed to initiate scan. Status Code: {response.status_code}'}), 500

@app.route('/get_artists_by_genre')
def get_artists_by_genre():
    genre = request.args.get('genre')
    if not genre:
        return jsonify({'error': 'Genre parameter is missing'}), 400

    destination_root = get_setting('Paths', 'destination_root')

    # Construct the path to the genre directory
    genre_path = os.path.join(destination_root, genre)

    # Fetch the list of artists/album artists from the genre directory
    try:
        artists = [name for name in os.listdir(genre_path) if os.path.isdir(os.path.join(genre_path, name))]
        return jsonify({'artists': artists}), 200
    except FileNotFoundError:
        return jsonify({'error': f"No such directory for genre '{genre}'"}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/jellyfin_check_artist', methods=['GET'])
def jellyfin_check_artist():
    artist_name = request.args.get('artist')
    if not artist_name:
        return "Artist name is required.", 400

    # Load Jellyfin API settings from config
    base_url = get_setting('Jellyfin', 'api_base_url')
    token = get_setting('Jellyfin', 'api_auth_token')
    userId = get_setting('Jellyfin', 'user_id')

    # Search for the artist in Jellyfin
    search_url = f"{base_url}/Artists?searchTerm={artist_name}&Limit=100&Fields=PrimaryImageAspectRatio,CanDelete,MediaSourceCount&Recursive=true&EnableTotalRecordCount=false&ImageTypeLimit=1&IncludePeople=false&IncludeMedia=false&IncludeGenres=false&IncludeStudios=false&IncludeArtists=true&userId={userId}"
    headers = {
        'Authorization': f'MediaBrowser Token={token}',
        'accept': 'application/json',
        'Content-Type': 'application/json'
    }

    # Send request to Jellyfin
    artist_response = requests.get(search_url, headers=headers)

    if artist_response.status_code != 200:
        return f"Error fetching artist details from Jellyfin: {artist_response.text}", artist_response.status_code

    artist_data = artist_response.json().get('Items', [])
    if not artist_data:
        return "No artist found in Jellyfin.", 404

    # Assume best match is the first one
    artist_id = artist_data[0].get('Id')

    # Get albums for the artist
    albums_url = f"{base_url}/Users/271ccedecfc548bdadc29720845d8e8d/Items?SortOrder=Descending,Descending,Ascending&IncludeItemTypes=MusicAlbum&Recursive=true&Fields=ParentId,PrimaryImageAspectRatio,ParentId,PrimaryImageAspectRatio&StartIndex=0&CollapseBoxSetItems=false&AlbumArtistIds={artist_id}&SortBy=PremiereDate,ProductionYear,Sortname"
    
    albums_response = requests.get(albums_url, headers=headers)
    
    if albums_response.status_code != 200:
        return f"Error fetching albums from Jellyfin: {albums_response.text}", albums_response.status_code

    albums_data = albums_response.json().get('Items', [])

    # Prepare album information to render the frontend
    album_info = []
    for album in albums_data:
        album_id = album['Id']
        image_tag = album['ImageTags'].get('Primary', None)

        album_info.append({
            'name': album['Name'],
            'id': album_id,
            'image_tag': image_tag,
            'link': f"/jellyfin_album_tracks?album_id={album_id}"  # Link to the album tracks
        })

    return render_template('jellyfin_artist_albums.html', artist=artist_data[0], albums=album_info, jellyfin_base_url=base_url)


# New endpoint to serve Jellyfin images via the backend to avoid CORS issues
@app.route('/jellyfin_image/<album_id>/<image_tag>', methods=['GET'])
def jellyfin_image(album_id, image_tag):
    # Load Jellyfin API settings from config
    base_url = get_setting('Jellyfin', 'api_base_url')
    token = get_setting('Jellyfin', 'api_auth_token')

    # Construct the image URL
    image_url = f"{base_url}/Items/{album_id}/Images/Primary?fillHeight=271&fillWidth=271&quality=96&tag={image_tag}"
    headers = {
        'Authorization': f'MediaBrowser Token={token}'
    }

    # Fetch the image from Jellyfin
    image_response = requests.get(image_url, headers=headers, stream=True)

    if image_response.status_code != 200:
        return f"Error fetching image from Jellyfin: {image_response.text}", image_response.status_code

    # Return the image content with appropriate headers
    return Response(image_response.content, content_type=image_response.headers['Content-Type'])


@app.route('/jellyfinalbumtracks/<string:album_id>', methods=['GET'])
def jellyfin_album_tracks(album_id):
    # Load Jellyfin API settings from config
    base_url = get_setting('Jellyfin', 'api_base_url')
    token = get_setting('Jellyfin', 'api_auth_token')

    headers = {
        'Authorization': f'MediaBrowser Token={token}',
        'accept': 'application/json',
        'Content-Type': 'application/json'
    }

    # Step 1: Get the album details
    album_url = f"{base_url}/Users/271ccedecfc548bdadc29720845d8e8d/Items/{album_id}"
    album_response = requests.get(album_url, headers=headers)
    
    if album_response.status_code != 200:
        return f"Error fetching album details from Jellyfin: {album_response.text}", album_response.status_code

    album_data = album_response.json()

    # Step 2: Fetch the tracks for the album
    tracks_url = f"{base_url}/Users/271ccedecfc548bdadc29720845d8e8d/Items?ParentId={album_id}&Fields=ItemCounts,PrimaryImageAspectRatio,CanDelete,MediaSourceCount&SortBy=ParentIndexNumber,IndexNumber,SortName"
    tracks_response = requests.get(tracks_url, headers=headers)

    if tracks_response.status_code != 200:
        return f"Error fetching album tracks from Jellyfin: {tracks_response.text}", tracks_response.status_code

    tracks_data = tracks_response.json().get('Items', [])

    # Prepare the track information for rendering
    track_info = []
    for track in tracks_data:
        track_info.append({
            'name': track['Name'],
            'id': track['Id'],
            'artist': ', '.join(track.get('Artists', [])),
            'runtime': track.get('RunTimeTicks', 0) / 10000000  # Convert ticks to seconds if available
        })

    # Step 3: Render the template with the complete album and track information
    return render_template('jellyfin_album_tracks.html', album=album_data, tracks=track_info)


# Define a helper function to safely access and modify active_downloads
def safe_update_command(command_id, updates):
    """Safely updates the active_downloads dictionary."""
    with command_lock:
        if command_id in active_downloads:
            active_downloads[command_id].update(updates)


# Modified function to execute a command and provide real-time output
def execute_command(command_id, command):
    """Function to execute a command using pexpect in a new thread."""
    safe_update_command(command_id, {'status': 'running'})
    logging.info(f"[{command_id}] Starting command: {command}")

    try:
        process = pexpect.spawn(command, encoding='utf-8', timeout=None)
        safe_update_command(command_id, {'process': process, 'output': []})

        process.logfile_read = sys.stdout

        while True:
            try:
                # Use read_nonblocking for real-time output
                output = process.read_nonblocking(size=1024, timeout=0.1)
                if output:
                    with command_lock:
                        # To handle carriage returns and screen clearing, we can process the output
                        # This is a simple implementation. A more robust one might be needed for complex cases.
                        cleaned_output = ansi_escape.sub('', output).strip()
                        if cleaned_output:
                             active_downloads[command_id]['output'].append(cleaned_output)
            except pexpect.exceptions.TIMEOUT:
                # No output received in the timeout period, continue loop
                if not process.isalive():
                    logging.info(f"[{command_id}] Process finished (detected by isalive).")
                    break
                continue
            except pexpect.exceptions.EOF:
                logging.info(f"[{command_id}] Process finished (EOF).")
                break
            except Exception as e:
                logging.error(f"[{command_id}] Error while reading command output: {str(e)}")
                with command_lock:
                    active_downloads[command_id]['output'].append(f"Error while reading command output: {str(e)}")
                break
        
        process.close()
        return_code = process.exitstatus
        signal_code = process.signalstatus
        
        logging.info(f"[{command_id}] Process exited with code: {return_code}, signal: {signal_code}")

        terminate_command(command_id)

        new_status = 'completed' if return_code == 0 else 'error'
        safe_update_command(command_id, {'status': new_status})

    except Exception as e:
        logging.error(f"[{command_id}] Failed to execute command: {e}")
        safe_update_command(command_id, {'status': 'error', 'output': [str(e)]})
    finally:
        logging.info(f"[{command_id}] Execution block finished. Running post-processing.")
        run_post_processing(command_id)

# Initialize the queue handler thread if not already running
def initialize_queue_handler():
    global queue_handler_thread
    if not queue_handler_thread or not queue_handler_thread.is_alive():
        queue_handler_thread = threading.Thread(target=queue_handler, daemon=True)
        queue_handler_thread.start()

# Queue handler function to check for running commands and start the next in line
def queue_handler():
    while True:
        time.sleep(5)  # Check every 5 seconds
        with download_lock:
            active_commands = [cmd for cmd in active_downloads.values() if cmd['status'] == 'running']
            if not active_commands and download_queue:
                next_command_id, next_command = download_queue.popleft()
                active_downloads[next_command_id]['status'] = 'running'
                # Start the command in a new thread
                thread = threading.Thread(target=execute_command, args=(next_command_id, next_command))
                thread.start()

# Revised download routes
def add_to_queue(command_id, command, download_info):
    with download_lock:
        download_queue.append((command_id, command))
        active_downloads[command_id] = download_info
    initialize_queue_handler()  # Make sure the queue handler is running



# Route to download a track
@app.route('/download_track', methods=['POST'])
def download_track():
    data = request.json
    artist_name = data.get('artistName')
    album_name = data.get('albumName')
    track_name = data.get('trackName')
    
    # Validate that both artist_name and track_name are provided
    if not artist_name or not track_name:
        return jsonify({'status': 'error', 'message': 'Artist name and track name are required.'}), 400

    command_id = str(uuid.uuid4())
    
    # Corrected call with proper variable names
    command = construct_track_download_command(sldlPath, artist_name, album_name, track_name, command_id)

    # Add to active downloads or queue
    download_info = {
        'command': command,
        'status': 'queued',
        'type': 'track',
        'artist_name': artist_name,
        'track_name': track_name,
        'output': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    add_to_queue(command_id, command, download_info)
    return jsonify({'status': 'success', 'command_id': command_id, 'message': 'Download added to queue.'}), 200


@app.route('/download_album', methods=['POST'])
def download_album():
    data = request.json
    artist_name = data.get('artistName')
    album_name = data.get('albumName')
    total_tracks = data.get('totalTracks')  # Extract total tracks from the request data

    # Validate required parameters
    if not artist_name or not album_name or not total_tracks:
        return jsonify({'status': 'error', 'message': 'Artist name, album name, and total tracks are required.'}), 400

    try:
        total_tracks = int(total_tracks)  # Ensure total_tracks is an integer
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Total tracks must be an integer.'}), 400


    command_id = str(uuid.uuid4())
    # Construct the command with dynamic total tracks
    command = construct_album_download_command(sldlPath, artist_name, album_name, total_tracks, command_id)
    
    # Add to active downloads or queue
    download_info = {
        'command': command,
        'status': 'queued',
        'type': 'album',
        'artist_name': artist_name,
        'album_name': album_name,
        'total_tracks': total_tracks,
        'output': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    add_to_queue(command_id, command, download_info)

    return jsonify({'status': 'success', 'command_id': command_id, 'message': 'Download added to queue.'}), 200


@app.route('/download_artist', methods=['POST'])
def download_artist():
    data = request.json
    artist_name = data.get('artistName')
    if not artist_name:
        return jsonify({'status': 'error', 'message': 'Artist name is required.'}), 400

    command_id = str(uuid.uuid4())    
    command = construct_artist_download_command(sldlPath, artist_name, command_id)


    # Add to active downloads or queue
    download_info = {
        'command': command,
        'status': 'queued',
        'type': 'artist',
        'artist_name': artist_name,
        'output': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    add_to_queue(command_id, command, download_info)
    return jsonify({'status': 'success', 'command_id': command_id, 'message': 'Download added to queue.'}), 200

# --- Helper Function (New) ---
def _process_playlist_download(playlist_url):
    """Core logic to process and queue a playlist download."""
    if not playlist_url:
        return {'status': 'error', 'message': 'Playlist URL is required.'}, 400

    playlist_name = "downloaded_playlist"
    if "spotify.com/playlist/" in playlist_url: # More robust check
        access_token = get_token()
        if not access_token:
            return {'status': 'error', 'message': 'Could not authenticate with Spotify.'}, 401

        # Corrected URL construction (removed extra spaces)
        playlist_id = playlist_url.split('/')[-1].split('?')[0]
        headers = {'Authorization': f'Bearer {access_token}'}
        spotify_playlist_url = f'https://api.spotify.com/v1/playlists/{playlist_id}' # Fixed URL
        try:
            playlist_response = requests.get(spotify_playlist_url, headers=headers)
            if playlist_response.status_code != 200:
                 logging.warning(f"Spotify API error ({playlist_response.status_code}) for playlist {playlist_id}: {playlist_response.text}")
                 # Fallback name if API fails
                 playlist_name = f'spotify_playlist_{playlist_id}'
            else:
                playlist_data = playlist_response.json()
                playlist_name = playlist_data.get('name', f'spotify_playlist_{playlist_id}')
        except Exception as e:
             logging.error(f"Exception fetching Spotify playlist name for {playlist_id}: {e}")
             # Fallback name if request fails
             playlist_name = f'spotify_playlist_{playlist_id}'

    elif "youtube.com/playlist?list=" in playlist_url or "youtube.com/watch?v=" in playlist_url: # Basic YouTube check
        try:
            # Use yt-dlp to get the playlist title
            result = subprocess.run(['yt-dlp', '--print', 'title', playlist_url], capture_output=True, text=True, check=True, timeout=30)
            fetched_name = result.stdout.strip()
            if fetched_name:
                playlist_name = fetched_name
            else:
                 logging.warning(f"yt-dlp returned empty title for {playlist_url}")
        except subprocess.TimeoutExpired:
            logging.warning(f"Timeout fetching YouTube playlist title for {playlist_url}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.warning(f"Failed to get playlist title from yt-dlp for {playlist_url}: {e}")
            # Fallback to a generic name if yt-dlp fails
            playlist_name = "youtube_playlist"
        except Exception as e:
             logging.error(f"Unexpected error fetching YouTube playlist name for {playlist_url}: {e}")
             playlist_name = "youtube_playlist"
    else:
         logging.warning(f"Unknown playlist URL format: {playlist_url}")
         playlist_name = "unknown_playlist"


    sanitized_playlist_name = re.sub(r'[^\w\s\-.\[\](){}]', '', playlist_name).strip() # Slightly more permissive sanitization
    # Handle case where sanitization removes everything
    if not sanitized_playlist_name:
        sanitized_playlist_name = f"playlist_{uuid.uuid4().hex[:8]}"

    download_path = get_setting('Paths', 'music_download_folder', '/app/downloads')
    output_path = os.path.join(download_path, sanitized_playlist_name)

    try:
        os.makedirs(output_path, exist_ok=True)
        marker_file_path = os.path.join(output_path, '.is_playlist')
        # Only create marker if it doesn't exist
        if not os.path.exists(marker_file_path):
            with open(marker_file_path, 'w') as f:
                pass
    except Exception as e:
        error_msg = f"Could not create playlist directory or marker file '{output_path}': {e}"
        logging.error(error_msg)
        return {'status': 'error', 'message': error_msg}, 500

    command_id = str(uuid.uuid4())
    command = construct_playlist_download_command(sldlPath, playlist_url, command_id)

    download_info = {
        'command': command,
        'status': 'queued',
        'type': 'playlist',
        'playlist_id': playlist_url, # Consider if storing the ID separately for Spotify is better
        'output': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    add_to_queue(command_id, command, download_info)

    return {'status': 'success', 'command_id': command_id, 'message': f'Download for playlist "{playlist_name}" added to queue.'}, 200


# --- Existing Route (Modified) ---
@app.route('/download_playlist', methods=['POST'])
def download_playlist():
    """Original endpoint expecting 'playlistUrl' in JSON."""
    data = request.json
    playlist_url = data.get('playlistUrl') # Expecting 'playlistUrl' key

    # Call the helper function
    response_data, status_code = _process_playlist_download(playlist_url)
    return jsonify(response_data), status_code




@app.route('/youtube')
def youtube():
    return render_template('youtube.html')


# Flask route to display the interactive download console
@app.route('/interactive_download_console/<command_id>', methods=['GET'])
def interactive_download_console(command_id):
    """Route to display the interactive download console for a specific command."""
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return "Command not found.", 404

    return render_template('interactive_download_console.html', command_id=command_id, command_info=command_info)

def _calculate_playlist_progress(command_id, output_lines):
    # logging.warning(f"[_calculate_playlist_progress] output_lines type: {type(output_lines).__name__}")
    # logging.warning(f"[_calculate_playlist_progress] output_lines demo: {output_lines[:5]}... (total {len(output_lines)} lines)")
    # logging.warning(f"[_calculate_playlist_progress] Join demo: {"/////".join(output_lines[:5])}")
    logging.info(f"[_calculate_playlist_progress] Calculating progress for command_id: {command_id}")

    if not output_lines:
        logging.warning(f"[_calculate_playlist_progress] No output lines for command_id: {command_id}")
        return {
            'overall_progress_percent': None,
            'completed_tracks': None,
            'total_tracks': None,
            'current_track_progress': None
        }

    total_tracks = None
    completed_tracks = 0
    current_track_progress = 0
    processed_tracks_identifiers = set() # Use a set to store unique identifiers

    # 2. Find completed tracks using the provided regex
    # Join lines to handle potential matches split across lines (though less likely here)
    # More importantly, this makes finding *all* matches easier.
    full_output_text = "\n".join(output_lines)

    # 1. Find total tracks by searching the entire output text
    # Look for "Downloading X tracks:" anywhere in the output
    total_tracks_match = re.search(r"Downloading\s+(\d+)\s+tracks\b", full_output_text)
    if total_tracks_match:
        total_tracks = int(total_tracks_match.group(1))
        logging.debug(f"[_calculate_playlist_progress] Found total tracks: {total_tracks}")
    else:
        logging.warning(f"[_calculate_playlist_progress] Could not find total tracks for command_id: {command_id} in full output.")
        # Couldn't find total tracks, cannot calculate
        return {
            'overall_progress_percent': None,
            'completed_tracks': None,
            'total_tracks': None,
            'current_track_progress': None
        }
        
    # Use the regex you provided to find all "Succeeded:" lines
    # The regex captures the part after "Succeeded:" but before the final "(100%"
    # We can use this captured part as a unique identifier for the track download attempt.
    success_pattern = re.compile(r"Succeeded:([^%]*?)\(100\s*%\)") # Non-greedy, capture group 1

    found_successes = success_pattern.findall(full_output_text)
    logging.debug(f"[_calculate_playlist_progress] Found {len(found_successes)} potential success markers.")

    for identifier_part in found_successes:
        # The identifier_part might contain the filename or user/share details.
        # Let's clean it slightly to make it a more robust key, focusing on the filename/user part.
        # Example identifier_part: "     soon-flower\\..\\04 Arde la ciudad.mp3 [253s/128kbps/3.9M "
        # We can strip whitespace and take a significant part, or hash it.
        # Stripping and using the core part should be sufficient for uniqueness in most cases.
        clean_identifier = identifier_part.strip()
        if clean_identifier and clean_identifier not in processed_tracks_identifiers:
             completed_tracks += 1
             processed_tracks_identifiers.add(clean_identifier)
             logging.warning(f"[_calculate_playlist_progress] Counted completed track: '{clean_identifier}'")

    logging.debug(f"[_calculate_playlist_progress] Total unique completed tracks counted: {completed_tracks}")

    # 3. Find latest current track progress
    # Look for the most recent line indicating the progress of *any* track
    # This regex looks for "InProgress:" followed by anything, then a number in parentheses with %
    progress_pattern = re.compile(r"InProgress:.*?\((\d+)\s*%\s*\)")
    # Search the full text again
    all_progress_matches = list(progress_pattern.finditer(full_output_text))
    if all_progress_matches:
        # Get the last match (most recent progress update)
        last_match = all_progress_matches[-1]
        try:
            current_track_progress = int(last_match.group(1))
            logging.debug(f"[_calculate_playlist_progress] Found latest track progress: {current_track_progress}%")
        except (ValueError, IndexError) as e:
            logging.warning(f"[_calculate_playlist_progress] Error parsing latest progress value: {e}")
    else:
         logging.warning(f"[_calculate_playlist_progress] No 'InProgress' lines found.")

    # 4. Calculate overall percentage
    overall_percentage = 0.0
    if total_tracks > 0:
        # Calculate progress including the current track's contribution
        # Use float division for precision
        overall_percentage = ((completed_tracks + (current_track_progress / 100.0)) / total_tracks) * 100
        # Ensure it doesn't exceed 100% due to potential timing issues or double counting
        overall_percentage = min(overall_percentage, 100.0)
        logging.debug(f"[_calculate_playlist_progress] Calculated overall percentage: {overall_percentage:.2f}%")

    return {
        'overall_progress_percent': round(overall_percentage, 2),
        'completed_tracks': completed_tracks,
        'total_tracks': total_tracks,
        'current_track_progress': current_track_progress
    }


# Example modification to the /update_console_output endpoint
# (Or create a new endpoint like /download_progress/<command_id>)
@app.route('/update_console_output/<command_id>', methods=['GET'])
def update_console_output(command_id):
    """Route to update the interactive console with the current output and progress of a command."""
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        logging.info(f"[update_console_output] Command not found: {command_id}")
        return jsonify({'error': 'Command not found'}), 404

    # Get the standard output and status
    output = command_info.get('output', [])
    status = command_info.get('status', 'unknown')

    # Prepare base response data
    response_data = {
        'output': output,
        'status': status
    }

    # --- Calculate and add progress for playlist downloads ---
    if command_info.get('type') == 'playlist' and status in ['running', 'completed', 'error']:
        logging.debug(f"[update_console_output] Calculating progress for playlist command: {command_id}")
        progress_data = _calculate_playlist_progress(command_id, output)
        # Add the calculated progress data to the response
        response_data.update(progress_data)
        logging.debug(f"[update_console_output] Progress data for {command_id}: {progress_data}")
    else:
        logging.debug(f"[update_console_output] Not a running playlist or type mismatch for command: {command_id}")

    return jsonify(response_data)

# Endpoint to send console input to the process
@app.route('/send_console_input/<command_id>', methods=['POST'])
def send_console_input(command_id):
    """Route to send input to an interactive process."""
    data = request.json
    user_input = data.get('input')

    if not user_input:
        return jsonify({'error': 'Input is required'}), 400

    # Access the process from the active_downloads dictionary
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info or 'process' not in command_info:
        return jsonify({'error': 'Command not found or not running'}), 404

    process = command_info['process']

    # Ensure the process is still alive
    if not process.isalive():
        return jsonify({'error': 'The process is not running anymore'}), 400

    try:
        # Send the input to the process
        process.sendline(user_input)
    except Exception as e:
        return jsonify({'error': f"Failed to send input: {str(e)}"}), 500

    return jsonify({'status': 'success', 'message': 'Input sent successfully'})



@app.route('/terminate_command/<command_id>', methods=['POST'])
def terminate_command(command_id):
    """Terminate the process associated with a command ID."""
    logging.info(f"[{command_id}] Received termination request.")
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        logging.warning(f"[{command_id}] Termination request for a command that does not exist.")
        return jsonify({'error': 'No command found with the given ID'}), 404

    if command_info['status'] == 'running':
        process = command_info.get('process')
        if process and process.isalive():
            logging.info(f"[{command_id}] Terminating running process (PID: {process.pid}).")
            try:
                process.terminate(force=True) # force=True sends SIGKILL
                logging.info(f"[{command_id}] Process terminated.")
            except Exception as e:
                logging.error(f"[{command_id}] Failed to terminate process: {str(e)}")
                return jsonify({'error': f"Failed to terminate process: {str(e)}"}), 500
        else:
            logging.info(f"[{command_id}] Process was already finished or doesn't exist.")

        safe_update_command(command_id, {'status': 'terminated'})
        logging.info(f"[{command_id}] Running post-processing after termination.")
        run_post_processing(command_id)
        return jsonify({'status': 'success', 'message': 'Process terminated successfully'})

    elif command_info['status'] == 'queued':
        logging.info(f"[{command_id}] Removing queued command.")
        with download_lock:
            download_queue[:] = [item for item in download_queue if item[0] != command_id]
        safe_update_command(command_id, {'status': 'terminated'})
        return jsonify({'status': 'success', 'message': 'Queued command terminated successfully'})

    logging.info(f"[{command_id}] No active process to terminate or command already completed.")
    return jsonify({'error': 'No active process to terminate or already completed'}), 400

@app.route('/complete_download/<command_id>', methods=['POST'])
def complete_download(command_id):
    """
    Handles the notification from sldl that a download is complete.
    This now triggers the termination sequence, which will run post-processing.
    """
    logging.info(f"[{command_id}] Received completion notification from sldl.")

    command_info = active_downloads.get(command_id)

    logging.info(f"[{command_id}] Command info: {command_info}")
    
    # It's crucial to terminate the command here to trigger the 'finally' block
    # in 'execute_command', which runs the post-processing.
    # NOTE: IT WILL BE TERMINATED WHEN PROCESS EXIT, TO PREVENT EARLY EXIT WHEN DOWNLOADING PLAYLIST
    # return terminate_command(command_id)
    return jsonify({'status': 'success', 'message': f'Command {command_id} marked as complete.'}), 200



# Route to view all active downloads
@app.route('/active_downloads', methods=['GET'])
def active_downloads_view():
    """Route to display the page with all active downloads."""
    with download_lock:
        running_downloads = [
            {
                'command_id': command_id,
                'type': download_info['type'],
                'artist_name': download_info.get('artist_name', 'N/A'),
                'track_album_name': download_info.get('track_name', download_info.get('album_name', 'N/A')),
                'status': download_info['status'],
                'start_time': download_info['start_time'],
                'link': url_for('interactive_download_console', command_id=command_id)
            }
            for command_id, download_info in active_downloads.items() if download_info['status'] == 'running'
        ]

        queued_downloads = [
            {
                'command_id': command_id,
                'type': download_info['type'],
                'artist_name': download_info.get('artist_name', 'N/A'),
                'track_album_name': download_info.get('track_name', download_info.get('album_name', 'N/A')),
                'status': download_info['status'],
                'start_time': download_info['start_time']
            }
            for command_id, download_info in active_downloads.items() if download_info['status'] == 'queued'
        ]

    # Render the active downloads page with both running and queued downloads
    return render_template('active_downloads.html', running_downloads=running_downloads, queued_downloads=queued_downloads)


# Route to download output
@app.route('/download_output/<command_id>', methods=['GET'])
def download_output(command_id):
    """Route to get the real-time output of a specific command."""
    with download_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return jsonify({'error': 'Command not found'}), 404

    return jsonify({'output': command_info['output'], 'status': command_info['status']})


# YouTube API using yt-dlp
@app.route('/api/youtube/search/playlist', methods=['POST'])
def youtube_search_playlist():
    data = request.json
    query = data.get('query')
    if not query:
        return jsonify({'error': 'Search query is required.'}), 400

    # URL encode the query and construct the search URL for playlists
    encoded_query = urllib.parse.quote(query)
    search_url = f"https://www.youtube.com/results?search_query={encoded_query}&sp=EgIQAw%253D%253D"

    # Command to get search results as JSON
    command = [
        'yt-dlp',
        '--dump-single-json',
        '--flat-playlist',
        search_url
    ]

    output = None
    try:
        logging.info(f"Running command: {' '.join(command)}")
        # Using subprocess.run to execute the command
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
        output = result.stdout
        logging.info(f"yt-dlp output: {output[:100]}...")  # Log the first 100 characters of the output for debugging
    except subprocess.CalledProcessError as e:
        # yt-dlp can exit with a non-zero code if some videos on the page are unavailable.
        # We can try to parse the output anyway.
        logging.warning(f"yt-dlp exited with an error, but we will try to parse its output. Stderr: {e.stderr}")
        if e.stdout:
            output = e.stdout
        else:
            logging.error(f"yt-dlp failed with no output. Stderr: {e.stderr}")
            return jsonify({'error': 'Failed to search YouTube.', 'details': e.stderr}), 500
    except FileNotFoundError:
        logging.error("yt-dlp command not found.")
        return jsonify({'error': 'yt-dlp is not installed or not in PATH.'}), 500
    except Exception as e:
        logging.error(f"An unexpected error occurred when running yt-dlp: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred.'}), 500

    if not output:
        return jsonify({'error': 'yt-dlp returned no output.'}), 500

    try:
        # Parse the JSON output
        search_data = json.loads(output)
        
        playlists = []
        if 'entries' in search_data:
            for entry in search_data['entries']:
                playlist_info = {
                    'title': entry.get('title', 'Unknown Title'),
                    'playlist_id': entry.get('id'),
                    'url': entry.get('url'),
                    'thumbnails': entry.get('thumbnails', []),
                    'ie_key': entry.get('ie_key')
                }
                
                # Add thumbnail URL for convenience (get the highest quality one)
                if playlist_info['thumbnails']:
                    # Sort by width to get highest quality
                    sorted_thumbnails = sorted(playlist_info['thumbnails'], 
                                             key=lambda x: x.get('width', 0), reverse=True)
                    playlist_info['thumbnail_url'] = sorted_thumbnails[0]['url']
                else:
                    playlist_info['thumbnail_url'] = None
                
                playlists.append(playlist_info)
        
        response_data = {
            'query': query,
            'total_results': len(playlists),
            'playlists': playlists,
            'search_url': search_url
        }
        
        return jsonify(response_data), 200


    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse yt-dlp JSON output: {e}")
        return jsonify({'error': 'Failed to parse YouTube search results.'}), 500
    except Exception as e:
        logging.error(f"An error occurred during YouTube search result processing: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred.'}), 500


@app.route('/api/youtube/playlist/details', methods=['POST'])
def youtube_playlist_details():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'Playlist URL is required.'}), 400

    command = [
        'yt-dlp',
        '--flat-playlist',
        '--dump-single-json',
        url
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
        
        # The output for a playlist is a single JSON object with an 'entries' key
        playlist_data = json.loads(result.stdout)
        
        videos = []
        for entry in playlist_data.get('entries', []):
            videos.append({
                'title': entry.get('title'),
                'url': f"https://www.youtube.com/watch?v={entry.get('id')}",
                'duration': entry.get('duration'),
                'artist': entry.get('uploader') # Best guess for artist
            })
            
        return jsonify({
            'playlist_title': playlist_data.get('title'),
            'videos': videos
        })

    except subprocess.CalledProcessError as e:
        logging.error(f"yt-dlp failed: {e.stderr}")
        return jsonify({'error': 'Failed to get playlist details from YouTube.', 'details': e.stderr}), 500
    except FileNotFoundError:
        logging.error("yt-dlp command not found.")
        return jsonify({'error': 'yt-dlp is not installed or not in PATH.'}), 500
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse yt-dlp JSON output: {e}")
        return jsonify({'error': 'Failed to parse YouTube playlist details.'}), 500
    except Exception as e:
        logging.error(f"An error occurred while fetching playlist details: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred.'}), 500

@app.route('/api/search/playlists', methods=['POST'])
def search_combined_playlists():
    data = request.json
    query = data.get('query')
    if not query:
        return jsonify({'error': 'Query parameter is required'}), 400

    spotify_results = []
    youtube_results = []

    # --- Spotify Search ---
    access_token = get_token()
    if access_token:
        headers = {'Authorization': f'Bearer {access_token}'}
        spotify_url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=playlist&limit=10"
        try:
            response = requests.get(spotify_url, headers=headers)
            if response.status_code == 200:
                try:
                    data = response.json()
                    # Safely handle the response structure
                    playlists_data = data.get('playlists', {})
                    if playlists_data and isinstance(playlists_data, dict):
                        items = playlists_data.get('items', [])
                        if isinstance(items, list):
                            for item in items:
                                if item and 'id' in item and 'name' in item and 'external_urls' in item:
                                    spotify_results.append({
                                        'source': 'spotify',
                                        'playlist_id': item['id'],
                                        'title': item['name'],
                                        'url': item['external_urls'].get('spotify', ''),
                                        'thumbnail_url': item['images'][0]['url'] if item.get('images') else None
                                    })
                except Exception as e:
                    logging.warning(f"Error processing Spotify API response: {e}")
            else:
                logging.warning(f"Spotify API returned status code {response.status_code}")
        except Exception as e:
            logging.warning(f"Spotify playlist search failed: {e}")

    # --- YouTube Search ---
    try:
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.youtube.com/results?search_query={encoded_query}&sp=EgIQAw%253D%253D"
        result = subprocess.run([
            'yt-dlp',
            '--dump-single-json',
            '--flat-playlist',
            '--max-downloads', '30',  # Limits to max 30 playlist entries
            search_url
        ], capture_output=True, text=True, encoding='utf-8')

        if result.stdout:
            try:
                search_data = json.loads(result.stdout)
                entries = search_data.get('entries', [])
                if isinstance(entries, list):
                    # Limit to first 30 results (in case max-downloads doesn't fully handle it)
                    for entry in entries[:30]:
                        if entry and 'id' in entry and 'title' in entry and 'url' in entry:
                            thumbnails = entry.get('thumbnails', [])
                            thumbnail_url = None
                            if thumbnails and isinstance(thumbnails, list):
                                sorted_thumbs = sorted(thumbnails,
                                                     key=lambda x: x.get('width', 0),
                                                     reverse=True)
                                if sorted_thumbs:
                                    thumbnail_url = sorted_thumbs[0].get('url')
                            youtube_results.append({
                                'source': 'youtube',
                                'playlist_id': entry['id'],
                                'title': entry['title'],
                                'url': entry['url'],
                                'thumbnail_url': thumbnail_url
                            })
            except json.JSONDecodeError:
                logging.warning("Failed to parse YouTube search results as JSON")
            except Exception as e:
                logging.warning(f"Error processing YouTube search results: {e}")
    except subprocess.TimeoutExpired:
        logging.warning(f"YouTube playlist search timed out after 20 seconds for query: {query}")
    except Exception as e:
        logging.warning(f"YouTube playlist search failed: {e}")

    # Combine results
    all_results = spotify_results + youtube_results

    return jsonify({
        'query': query,
        'results': all_results
    }), 200

# --- Unified Endpoint (Modified to use helper) ---
# (Assuming this function exists in your code as defined previously)
@app.route('/api/download/playlist', methods=['POST'])
def download_playlist_unified(): # Or whatever you named it
    """Unified endpoint accepting source and playlist_id/playlist_url."""
    data = request.json
    source = data.get('source')
    playlist_id = data.get('playlist_id')
    playlist_url = data.get('playlist_url', '') # Accept optional full URL

    if not source or source not in ['spotify', 'youtube']:
        return jsonify({'status': 'error', 'message': 'Source must be "spotify" or "youtube"'}), 400

    if not playlist_id and not playlist_url:
        return jsonify({'status': 'error', 'message': 'Either playlist_id or playlist_url is required'}), 400

    # Construct URL if not provided or if ID is given
    if not playlist_url:
        if source == 'spotify':
            # Standard Spotify playlist URL format
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        elif source == 'youtube':
            # Standard YouTube playlist URL format
            # Note: If playlist_id was a video ID from a playlist URL,
            # this might need parsing. Assuming it's the playlist list ID.
            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

    # --- Call the core processing function ---
    response_data, status_code = _process_playlist_download(playlist_url)
    return jsonify(response_data), status_code

# ... (previous imports and code) ...

@app.route('/api/spotify/playlist/<playlist_id>', methods=['GET'])
def api_get_spotify_playlist_details(playlist_id):
    """
    API endpoint to fetch and return the title, and track details (name and primary artist) 
    of a Spotify playlist.
    
    Args:
        playlist_id (str): The Spotify ID of the playlist.

    Returns:
        JSON: A JSON object containing 'title' and 'tracks' (list of dicts with 'name' and 'artist') on success (200),
              or an error message and appropriate status code (401, 404, 500).
    """
    logging.info(f"[api_get_spotify_playlist_details] Fetching details for playlist ID: {playlist_id}")
    
    # Get a valid Spotify token (user token if available, otherwise client credentials)
    access_token = get_token()
    if not access_token:
        logging.error("[api_get_spotify_playlist_details] Could not authenticate with Spotify.")
        return jsonify({'error': 'Could not authenticate with Spotify.'}), 401

    headers = {'Authorization': f'Bearer {access_token}'}
    
    # --- 1. Fetch Playlist Title ---
    playlist_details_url = f'https://api.spotify.com/v1/playlists/{playlist_id}'
    try:
        playlist_details_response = requests.get(playlist_details_url, headers=headers)
        
        if playlist_details_response.status_code == 200:
            playlist_data = playlist_details_response.json()
            playlist_title = playlist_data.get('name', f'Untitled Playlist ({playlist_id})')
            logging.debug(f"[api_get_spotify_playlist_details] Playlist title: {playlist_title}")
        elif playlist_details_response.status_code == 404:
            logging.warning(f"[api_get_spotify_playlist_details] Playlist with ID {playlist_id} not found.")
            return jsonify({'error': 'Playlist not found.'}), 404
        else:
            error_msg = f"Spotify API error ({playlist_details_response.status_code}) fetching playlist details: {playlist_details_response.text}"
            logging.error(f"[api_get_spotify_playlist_details] {error_msg}")
            return jsonify({'error': 'Failed to fetch playlist details from Spotify.', 'details': error_msg}), 500
            
    except requests.exceptions.RequestException as e:
        logging.error(f"[api_get_spotify_playlist_details] Network error fetching playlist details: {e}")
        return jsonify({'error': 'Network error occurred while fetching playlist details.'}), 500
    except json.JSONDecodeError as e:
        logging.error(f"[api_get_spotify_playlist_details] Error decoding JSON for playlist details: {e}")
        return jsonify({'error': 'Error processing playlist details response from Spotify.'}), 500
    except Exception as e:
        logging.error(f"[api_get_spotify_playlist_details] Unexpected error fetching playlist details: {e}")
        return jsonify({'error': 'An unexpected error occurred while fetching playlist details.'}), 500

    # --- 2. Fetch Playlist Tracks ---
    tracks_url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100' # Use limit=100 for efficiency
    track_details = [] # List to store dictionaries with name and artist
    
    try:
        while tracks_url:
            tracks_response = requests.get(tracks_url, headers=headers)
            
            if tracks_response.status_code == 200:
                tracks_data = tracks_response.json()
                
                # Extract track names and primary artists, handling potential missing data
                items = tracks_data.get('items', [])
                for item in items:
                    track = item.get('track')
                    if track: # Check if track object exists
                        track_name = track.get('name', "Unknown Track")
                        
                        # Get the first artist's name if available
                        artists = track.get('artists', [])
                        primary_artist_name = "Unknown Artist"
                        if artists and isinstance(artists, list) and 'name' in artists[0]:
                            primary_artist_name = artists[0]['name']
                            
                        track_details.append({
                            'name': track_name,
                            'artist': primary_artist_name
                        })
                    else:
                        # Handle items that aren't tracks (e.g., local files sometimes appear differently)
                        logging.debug(f"[api_get_spotify_playlist_details] Found non-track item in playlist {playlist_id}.")
                        # Appending a placeholder maintains list consistency
                        track_details.append({
                            'name': "Non-Track Item",
                            'artist': "N/A"
                        })
                        
                # Check if there are more pages of tracks
                tracks_url = tracks_data.get('next') # This will be None if no more pages
                logging.debug(f"[api_get_spotify_playlist_details] Fetched page. Next URL: {tracks_url is not None}")
                
            elif tracks_response.status_code == 404:
                 # This should ideally not happen if the playlist exists, but handle it
                 logging.warning(f"[api_get_spotify_playlist_details] Tracks endpoint returned 404 for playlist {playlist_id}.")
                 return jsonify({'error': 'Playlist tracks not found.'}), 404
            else:
                error_msg = f"Spotify API error ({tracks_response.status_code}) fetching tracks: {tracks_response.text}"
                logging.error(f"[api_get_spotify_playlist_details] {error_msg}")
                # If we already have the title, we could return partial data or an error.
                # Returning an error is generally cleaner if tracks fail.
                return jsonify({
                    'title': playlist_title, 
                    'tracks': track_details, # Return tracks fetched so far
                    'error': 'Failed to fetch all playlist tracks from Spotify.',
                    'details': error_msg
                }), 500 
                
    except requests.exceptions.RequestException as e:
        logging.error(f"[api_get_spotify_playlist_details] Network error fetching tracks: {e}")
        return jsonify({
            'title': playlist_title, 
            'tracks': track_details, # Return tracks fetched so far
            'error': 'Network error occurred while fetching playlist tracks.',
            'details': str(e)
        }), 500
    except json.JSONDecodeError as e:
        logging.error(f"[api_get_spotify_playlist_details] Error decoding JSON for tracks: {e}")
        return jsonify({
            'title': playlist_title, 
            'tracks': track_details,
            'error': 'Error processing playlist tracks response from Spotify.',
            'details': str(e)
        }), 500
    except Exception as e:
        logging.error(f"[api_get_spotify_playlist_details] Unexpected error fetching tracks: {e}")
        return jsonify({
            'title': playlist_title, 
            'tracks': track_details,
            'error': 'An unexpected error occurred while fetching playlist tracks.',
            'details': str(e)
        }), 500

    # --- 3. Return Success Response ---
    logging.info(f"[api_get_spotify_playlist_details] Successfully fetched {len(track_details)} tracks for playlist '{playlist_title}'.")
    return jsonify({
        'title': playlist_title,
        'tracks': track_details 
    }), 200



if __name__ == '__main__':
    # Generate sldl.conf on startup
    generate_sldl_config()
    
    # Get SSL settings from config
    ssl_cert = get_setting('Server', 'ssl_cert_path', fallback=None)
    ssl_key = get_setting('Server', 'ssl_key_path', fallback=None)
    ssl_context = None
    if ssl_cert and ssl_key:
        ssl_context = (ssl_cert, ssl_key)

    # Run the Flask app
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, ssl_context=ssl_context)
