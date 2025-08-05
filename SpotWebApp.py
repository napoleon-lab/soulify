from flask import Flask, redirect, request, session, url_for, render_template, jsonify, Response, stream_with_context
from CommandConstruct import (construct_track_download_command,
                              construct_album_download_command,
                              construct_artist_download_command,
                              construct_playlist_download_command)
from datetime import datetime
import requests
import base64
import urllib.parse
import os
import subprocess
import logging
import time
import uuid
import configparser
import shlex
from threading import Thread
import re
import threading
import shutil
import mutagen
import queue
import pexpect
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON, TPE1, TALB
from mutagen.mp4 import MP4
from mutagen.wavpack import WavPack
import stat
from collections import deque

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
soulify_conf_path = os.path.join(base_dir, 'soulify.conf')
postdownload_scripts_dir = os.path.join(base_dir, 'scripts', 'postdownload')
run_all_script = os.path.join(postdownload_scripts_dir, 'RunAll.py')
sort_move_music_script = os.path.join(postdownload_scripts_dir, 'Sort_MoveMusicDownloads.py')
update_with_mb_script = os.path.join(postdownload_scripts_dir, 'UpdatewithMB.sh')
pdscript_conf_path = os.path.join(base_dir, 'pdscript.conf')

ansi_escape = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')


# Function to read the soulify.conf file
def read_soulify_conf():
    config = configparser.ConfigParser()
    config.read(soulify_conf_path)
    try:
        update_with_mb = config.getboolean('PostDownloadProcessing', 'UpdatemetadataWithMusicBrainz', fallback=True)
        update_library = config.getboolean('PostDownloadProcessing', 'UpdateLibraryMetadataAndRefreshJellyfin', fallback=True)
        return update_with_mb, update_library
    except configparser.Error as e:
        logging.error(f"Error reading configuration: {e}")
        return False, False  # Fallback to False if there's an error
        
# Function to write soulify.conf (missing in original code)
def write_soulify_conf(settings):
    config = configparser.ConfigParser()
    config['PostDownloadProcessing'] = {
        'UpdatemetadataWithMusicBrainz': str(settings['UpdatemetadataWithMusicBrainz']),
        'UpdateLibraryMetadataAndRefreshJellyfin': str(settings['UpdateLibraryMetadataAndRefreshJellyfin'])
    }
    try:
        with open(soulify_conf_path, 'w') as configfile:
            config.write(configfile)
    except IOError as e:
        logging.error(f"Error writing to soulify.conf: {e}")

# Function to write sldl.conf
def write_sldl_conf(settings):
    try:
        with open(sldlConfigPath, 'w') as f:
            f.write("# Soulseek Credentials (required)\n")
            f.write(f"username = {settings.get('username', '')}\n")
            f.write(f"password = {settings.get('password', '')}\n")
            f.write("\n# General Download Settings\n")
            f.write(f"path = {settings.get('path', '')}\n")
            f.write("\n# Search Settings\n")
            f.write(f"no-remove-special-chars = {settings.get('no-remove-special-chars', 'false')}\n")
            f.write("\n# Preferred File Conditions\n")
            f.write(f"pref-format = {settings.get('pref-format', '')}\n")
            f.write("\n# Spotify Settings\n")
            f.write(f"spotify-id = {settings.get('spotify-id', '')}\n")
            f.write(f"spotify-secret = {settings.get('spotify-secret', '')}\n")
            f.write("\n# Output Settings\n")
            f.write(f"m3u = {settings.get('m3u', 'none')}\n")
    except IOError as e:
        logging.error(f"Error writing to sldl.conf: {e}")
        
# Function to read pdscript.conf
def read_pdscript_conf():
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    try:
        settings = {
            'destination_root': config.get('Paths', 'destination_root', fallback='/mnt/EXTHDD/Media/Audio/Music/Music - Managed (Lidarr)/'),
            'new_artists_dir': config.get('Paths', 'new_artists_dir', fallback='/mnt/EXTHDD/Download/Music New Artists/'),
            'api_base_url': config.get('API Details', 'API_BASE_URL', fallback='http://192.168.0.7:8096'),
            'api_auth_token': config.get('API Details', 'API_AUTH_TOKEN', fallback='b30092879a9646eb9e676b2922c9c1e4'),
            'main_music_library_id': config.get('API Details', 'main_music_library_id', fallback='7e64e319657a9516ec78490da03edccb')
        }
        return settings
    except configparser.Error as e:
        logging.error(f"Error reading pdscript.conf: {e}")
        return {}


# Function to write pdscript.conf
def write_pdscript_conf(settings):
    config = configparser.ConfigParser()
    config['Paths'] = {
        'destination_root': settings.get('destination_root', '/mnt/EXTHDD/Media/Audio/Music/Music - Managed (Lidarr)/'),
        'new_artists_dir': settings.get('new_artists_dir', '/mnt/EXTHDD/Download/Music New Artists/')
    }
    config['API Details'] = {
        'API_BASE_URL': settings.get('API_BASE_URL', 'http://192.168.0.7:8096'),
        'API_AUTH_TOKEN': settings.get('API_AUTH_TOKEN', 'b30092879a9646eb9e676b2922c9c1e4')
    }
    try:
        with open(pdscript_conf_path, 'w') as configfile:
            config.write(configfile)
    except IOError as e:
        logging.error(f"Error writing to pdscript.conf: {e}")

def clean_special_chars(query):
    # First remove all commas
    query = query.replace(',', '')
    # Then remove all other special characters except for alphanumeric, spaces, and hyphens
    return re.sub(r'[^a-zA-Z0-9\s\-]', '', query)


# Function to execute post-processing scripts based on the configuration
def run_post_processing():
    update_with_mb, update_library = read_soulify_conf()

    # Check conditions and run the appropriate scripts
    if update_library and update_with_mb:
        # Run RunAll.py
        logging.info("Running RunAll.py script...")
        result = subprocess.run(['python3', run_all_script], capture_output=True, text=True)
        log_post_processing_output(result, 'RunAll.py')
    elif update_library and not update_with_mb:
        # Run Sort_MoveMusicDownloads.py
        logging.info("Running Sort_MoveMusicDownloads.py script...")
        result = subprocess.run(['python3', sort_move_music_script], capture_output=True, text=True)
        log_post_processing_output(result, 'Sort_MoveMusicDownloads.py')
    elif update_with_mb and not update_library:
        # Run UpdatewithMB.sh
        logging.info("Running UpdatewithMB.sh script...")
        result = subprocess.run(['bash', update_with_mb_script], capture_output=True, text=True)
        log_post_processing_output(result, 'UpdatewithMB.sh')
    else:
        # No post-processing needed
        logging.info("No post-processing scripts to run.")

# Load the Spotify settings from sldl.conf
def parse_spotify_conf(file_path):
    spotify_settings = {}
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                key_value = line.split('=', 1)
                if len(key_value) == 2:
                    key, value = key_value[0].strip(), key_value[1].strip()
                    spotify_settings[key] = value
    except IOError as e:
        logging.error(f"Error reading config file {file_path}: {e}")
    return spotify_settings



# Dynamically construct the paths based on the location of SpotWebApp.py
base_dir = os.path.dirname(os.path.abspath(__file__))
sldlPath = os.path.join(base_dir, 'sldl')
sldlConfigPath = os.path.join(base_dir, 'sldl.conf')
spotifyauthConfigPath = os.path.join(base_dir, 'spotifyauth.conf')

# Now that parse_spotify_conf is defined, you can call it
spotify_config = parse_spotify_conf(spotifyauthConfigPath)
CLIENT_ID = spotify_config.get('spotify-id')
CLIENT_SECRET = spotify_config.get('spotify-secret')
REDIRECT_URI = spotify_config.get('redirect-uri')

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

@app.route('/')
def index():
    """Home route."""
    access_token = ensure_valid_token()  # Check for valid token and refresh if needed
    if not access_token:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/playlists')
def playlists():
    """List the user's Spotify playlists."""
    access_token = ensure_valid_token()
    if not access_token:
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {access_token}'}
    url = "https://api.spotify.com/v1/me/playlists?limit=50&offset=0"
    playlists = []
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            playlists.extend(data['items'])
            url = data['next']  # Next page URL
        else:
            return f"Error fetching playlists: {response.json()}"

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
        # Process form submission for sldl.conf
        sldl_settings = {
            'username': request.form.get('username'),
            'password': request.form.get('password'),
            'path': request.form.get('path'),
            'no-remove-special-chars': request.form.get('no-remove-special-chars'),
            'pref-format': request.form.get('pref-format'),
            'spotify-id': request.form.get('spotify-id'),
            'spotify-secret': request.form.get('spotify-secret'),
            'm3u': request.form.get('m3u')
        }
        write_sldl_conf(sldl_settings)

        # Process form submission for soulify.conf
        soulify_settings = {
            'UpdatemetadataWithMusicBrainz': request.form.get('UpdatemetadataWithMusicBrainz') == 'true',
            'UpdateLibraryMetadataAndRefreshJellyfin': request.form.get('UpdateLibraryMetadataAndRefreshJellyfin') == 'true'
        }
        write_soulify_conf(soulify_settings)

        # Process form submission for pdscript.conf
        pdscript_settings = {
            'destination_root': request.form.get('destination_root'),
            'new_artists_dir': request.form.get('new_artists_dir'),
            'API_BASE_URL': request.form.get('api_base_url'),
            'API_AUTH_TOKEN': request.form.get('api_auth_token')
        }
        write_pdscript_conf(pdscript_settings)

        return redirect(url_for('settings'))

    # For GET request, display current settings
    sldl_settings = parse_sldl_conf(sldlConfigPath)
    soulify_settings = read_soulify_conf()
    pdscript_settings = read_pdscript_conf()

    return render_template('settings.html', sldl=sldl_settings, soulify=soulify_settings, pdscript=pdscript_settings)

@app.route('/post_download_management')
def post_download_management():
    return render_template('post_download_management.html')

@app.route('/move_artist_folder', methods=['POST'])
def move_artist_folder():
    artist_name = request.form['artistName']
    genre = request.form['genre']
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    new_artists_dir = config.get('Paths', 'new_artists_dir')
    destination_root = config.get('Paths', 'destination_root')

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
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    try:
        # Retrieve paths from the configuration
        new_artists_dir = config.get('Paths', 'new_artists_dir')
        destination_root = config.get('Paths', 'destination_root')

        # List all folders at the root of the destination_root and sort them
        genres = [folder for folder in os.listdir(destination_root) if os.path.isdir(os.path.join(destination_root, folder))]
        genres.sort(key=lambda s: s.strip().upper())  # Strip whitespace and sort ignoring case

    except configparser.Error as e:
        logging.error(f"Error reading pdscript.conf: {e}")
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
    # Read the configuration file
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)

    # Get the unknown albums directory path from the configuration
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir')

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
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)

    # Retrieve the unknown albums directory path from the configuration
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir')

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
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    
    # Retrieve paths from the configuration
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir')
    destination_root = config.get('Paths', 'destination_root')

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
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir')
    
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
        config = configparser.ConfigParser()
        config.read(pdscript_conf_path)
        unknown_albums_dir = config.get('Paths', 'unknown_albums_dir')
        destination_root = config.get('Paths', 'destination_root')

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
    settings = read_pdscript_conf()
    destination_root = settings['destination_root']
    
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
    settings = read_pdscript_conf()
    destination_root = settings['destination_root']

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
    settings = read_pdscript_conf()
    url = f"{settings['api_base_url']}/Items/{settings['main_music_library_id']}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
    headers = {
        'Authorization': f"MediaBrowser Token={settings['api_auth_token']}",
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

    # Assuming 'destination_root' is stored in a configuration or to be set as a constant
    config = read_pdscript_conf()  # Or however you fetch your configuration
    destination_root = config['destination_root']  # Ensure this is the correct key

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

# Load the pdscript configuration to get Jellyfin details
def get_pdscript_config():
    config = configparser.ConfigParser()
    config.read(pdscript_conf_path)
    return {
        'api_base_url': config.get('API Details', 'API_BASE_URL'),
        'api_auth_token': config.get('API Details', 'API_AUTH_TOKEN')
    }

@app.route('/jellyfin_check_artist', methods=['GET'])
def jellyfin_check_artist():
    artist_name = request.args.get('artist')
    if not artist_name:
        return "Artist name is required.", 400

    # Load Jellyfin API settings from config
    jellyfin_config = get_pdscript_config()
    base_url = jellyfin_config['api_base_url']
    token = jellyfin_config['api_auth_token']

    # Search for the artist in Jellyfin
    search_url = f"{base_url}/Artists?searchTerm={artist_name}&Limit=100&Fields=PrimaryImageAspectRatio,CanDelete,MediaSourceCount&Recursive=true&EnableTotalRecordCount=false&ImageTypeLimit=1&IncludePeople=false&IncludeMedia=false&IncludeGenres=false&IncludeStudios=false&IncludeArtists=true&userId=271ccedecfc548bdadc29720845d8e8d"
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
    jellyfin_config = get_pdscript_config()
    base_url = jellyfin_config['api_base_url']
    token = jellyfin_config['api_auth_token']

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
    jellyfin_config = get_pdscript_config()
    base_url = jellyfin_config['api_base_url']
    token = jellyfin_config['api_auth_token']

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
    # Mark the command as running
    safe_update_command(command_id, {'status': 'running'})

    try:
        # Start the interactive process with pexpect
        process = pexpect.spawn(command, encoding='utf-8', timeout=None)

        # Attach the process to active_downloads for interaction
        safe_update_command(command_id, {'process': process, 'output': []})

        # Read stdout and stderr output line by line
        while True:
            try:
                # Readline reads the output as it becomes available
                output = process.readline().strip()
                if output:
                    with command_lock:
                        active_downloads[command_id]['output'].append(output)

            except pexpect.exceptions.EOF:
                break  # Process finished
            except Exception as e:
                with command_lock:
                    active_downloads[command_id]['output'].append(f"Error while reading command output: {str(e)}")

        # Determine the final status of the command
        return_code = process.exitstatus
        new_status = 'completed' if return_code == 0 else 'error'
        safe_update_command(command_id, {'status': new_status})

    finally:
        # Ensure the process is properly terminated
        if process.isalive():
            process.terminate()

        # After command termination or completion, run post-processing
        run_post_processing()

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
    command = construct_track_download_command(sldlPath, sldlConfigPath, artist_name, album_name, track_name, command_id)

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
    command = construct_album_download_command(sldlPath, sldlConfigPath, artist_name, album_name, total_tracks, command_id)
    
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
    command = construct_artist_download_command(sldlPath, sldlConfigPath, artist_name, command_id)


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

@app.route('/download_playlist', methods=['POST'])
def download_playlist():
    data = request.json
    playlist_id = data.get('playlistId')
    if not playlist_id:
        return jsonify({'status': 'error', 'message': 'Playlist ID is required.'}), 400

    command_id = str(uuid.uuid4())
    command = construct_playlist_download_command(sldlPath, sldlConfigPath, playlist_id, command_id)


    # Add to active downloads or queue
    download_info = {
        'command': command,
        'status': 'queued',
        'type': 'playlist',
        'playlist_id': playlist_id,
        'output': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    add_to_queue(command_id, command, download_info)
    return jsonify({'status': 'success', 'command_id': command_id, 'message': 'Download added to queue.'}), 200

# Flask route to display the interactive download console
@app.route('/interactive_download_console/<command_id>', methods=['GET'])
def interactive_download_console(command_id):
    """Route to display the interactive download console for a specific command."""
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return "Command not found.", 404

    return render_template('interactive_download_console.html', command_id=command_id, command_info=command_info)


# Endpoint to get real-time output from the process
@app.route('/update_console_output/<command_id>', methods=['GET'])
def update_console_output(command_id):
    """Route to update the interactive console with the current output of a command."""
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return jsonify({'error': 'Command not found'}), 404

    return jsonify({'output': command_info['output'], 'status': command_info['status']})

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
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return jsonify({'error': 'No command found with the given ID'}), 404

    if command_info['status'] == 'running':
        # Terminate the running process
        process = command_info.get('process')
        try:
            if process.isalive():
                process.terminate()
            safe_update_command(command_id, {'status': 'terminated'})

            # Run post-processing after the termination of the command
            run_post_processing()

            return jsonify({'status': 'success', 'message': 'Process terminated successfully'})
        except Exception as e:
            return jsonify({'error': f"Failed to terminate process: {str(e)}"}), 500

    elif command_info['status'] == 'queued':
        # Remove from queue
        with download_lock:
            for idx, (queued_id, _) in enumerate(download_queue):
                if queued_id == command_id:
                    del download_queue[idx]
                    break
            safe_update_command(command_id, {'status': 'terminated'})


        return jsonify({'status': 'success', 'message': 'Queued command terminated successfully'})

    return jsonify({'error': 'No active process to terminate or already completed'}), 400

@app.route('/complete_download/<command_id>', methods=['POST'])
def complete_download(command_id):
    """Handles the notification when a download completes and triggers termination."""
    with command_lock:
        command_info = active_downloads.get(command_id)

    if not command_info:
        return jsonify({'error': 'No command found with the given ID'}), 404

    if command_info['status'] == 'running':
        # Call terminate_command to terminate the process
        return terminate_command(command_id)
    elif command_info['status'] == 'queued':
        # If the command was still queued, it should be removed from the queue
        with download_lock:
            for idx, (queued_id, _) in enumerate(download_queue):
                if queued_id == command_id:
                    del download_queue[idx]
                    break
            safe_update_command(command_id, {'status': 'terminated'})

        return jsonify({'status': 'success', 'message': f'Queued command {command_id} marked as completed and removed'}), 200

    return jsonify({'error': 'No active process to terminate or already completed'}), 400



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

if __name__ == '__main__':
    # Run the Flask app
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
