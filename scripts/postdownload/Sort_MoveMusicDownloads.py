import os
import shutil
import logging
import subprocess
import requests  # For making API calls
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
import configparser
import sys
import time
import hashlib
import json

# Setup logging
script_dir = os.path.dirname(os.path.realpath(__file__))
log_file = os.path.join(script_dir, "music_sorting_debug.log")
# Set up logging to stdout (for INFO/DEBUG) and stderr (for ERROR+)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout)  # All levels to stdout
    ]
)

# Path to the config file (2 folder levels above the script)
config_dir = os.path.abspath(os.path.join(script_dir, "../../"))
config_path = os.path.join(config_dir, 'config.ini')

# Global variables for Navidrome config
navidrome_url = None
navidrome_user = None
navidrome_password = None

# Function to read configurations from config.ini
def load_config():
    global navidrome_url, navidrome_user, navidrome_password
    
    if not os.path.exists(config_path):
        logging.error(f"config.ini not found at {config_path}. Please ensure it exists.")
        raise FileNotFoundError(f"config.ini not found at {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    # [Paths]
    source_route = config.get('Paths', 'source_route').strip()
    destination_root = config.get('Paths', 'destination_root').strip()
    playlist_dir = config.get('Paths', 'playlist_dir').strip()
    new_artists_dir = config.get('Paths', 'new_artists_dir').strip()
    music_download_folder = config.get('Paths', 'music_download_folder').strip()
    unknown_albums_dir = config.get('Paths', 'unknown_albums_dir').strip()
    download_path = music_download_folder  # Formerly from sldl.conf

    # [Navidrome] - Optional section
    try:
        navidrome_url = config.get('Navidrome', 'url').strip()
        navidrome_user = config.get('Navidrome', 'user').strip()
        navidrome_password = config.get('Navidrome', 'password').strip()
        logging.info(f"Navidrome config loaded: URL={navidrome_url}, User={navidrome_user}")
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        logging.warning(f"Navidrome config not found or incomplete: {e}")
        navidrome_url = navidrome_user = navidrome_password = None

    logging.debug(f"Config loaded: source_route={source_route}, destination_root={destination_root}, "
                  f"new_artists_dir={new_artists_dir}, music_download_folder={music_download_folder}, "
                  f"unknown_albums_dir={unknown_albums_dir}")

    return (source_route, destination_root, new_artists_dir, music_download_folder, 
            unknown_albums_dir, playlist_dir, download_path)

# Load configuration variables
(source_route, destination_root, new_artists_dir, music_download_folder, 
 unknown_albums_dir, playlist_dir, download_path) = load_config()

# Audio file extensions to check
audio_extensions = {'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.wav', '.ogg', '.wma', '.alac', '.aiff', '.opus'}

# Function to get Navidrome authentication token
def get_navidrome_token():
    """Get authentication token from Navidrome API"""
    if not all([navidrome_url, navidrome_user, navidrome_password]):
        logging.warning("Navidrome credentials not configured, skipping API calls")
        return None, None
    
    try:
        login_url = f"{navidrome_url}/auth/login"
        login_data = {
            "username": navidrome_user,
            "password": navidrome_password
        }
        
        response = requests.post(login_url, json=login_data, timeout=10)
        response.raise_for_status()
        
        auth_data = response.json()
        token = auth_data.get('subsonicToken')
        salt = auth_data.get('subsonicSalt')
        
        if token and salt:
            logging.info("Successfully obtained Navidrome authentication token")
            return token, salt
        else:
            logging.error("Failed to get token/salt from Navidrome response")
            return None, None
            
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to authenticate with Navidrome: {e}")
        return None, None
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse Navidrome auth response: {e}")
        return None, None

def trigger_navidrome_scan():
    """Trigger a full scan on Navidrome after playlist creation"""
    if not all([navidrome_url, navidrome_user, navidrome_password]):
        logging.info("Navidrome not configured, skipping scan trigger")
        return
    
    token, salt = get_navidrome_token()
    if not token or not salt:
        logging.error("Could not get Navidrome token, skipping scan")
        return
    
    try:
        scan_url = f"{navidrome_url}/rest/startScan"
        params = {
            'u': navidrome_user,
            't': token,
            's': salt,
            'f': 'json',
            'v': '1.8.0',
            'c': 'NavidromeUI',
            'fullScan': 'true'
        }
        
        response = requests.get(scan_url, params=params, timeout=30)
        response.raise_for_status()
        
        logging.info("✓ Successfully triggered Navidrome full scan")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to trigger Navidrome scan: {e}")

# Function to set permissions
def set_permissions(path):
    try:
        os.chmod(path, 0o777)
        logging.info(f"Set permissions to 777 for {path}")
    except Exception as e:
        logging.error(f"Failed to set permissions for {path}: {e}")

# Function to check for audio files and .incomplete files in a folder
def folder_contains_audio_or_incomplete(folder_path):
    contains_audio = False
    contains_incomplete = False
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_ext = os.path.splitext(file)[1].lower()
            if file_ext in audio_extensions:
                contains_audio = True
            if file_ext == '.incomplete':
                contains_incomplete = True
            if contains_incomplete:
                break  # If .incomplete file found, break early
        if contains_incomplete:
            break
    return contains_audio, contains_incomplete

# Function to move files and check size
def move_and_compare(src_file, dst_file):
    logging.debug(f"Comparing source file: {src_file} with destination file: {dst_file}")
    src_size = os.path.getsize(src_file)
    if os.path.exists(dst_file):
        dst_size = os.path.getsize(dst_file)
        logging.debug(f"Source file size: {src_size}, Destination file size: {dst_size}")
        if src_size > dst_size:
            os.remove(dst_file)
            shutil.move(src_file, dst_file)
            logging.info(f"Moved larger file from {src_file} to {dst_file}")
        else:
            os.remove(src_file)
            logging.info(f"Deleted smaller file {src_file} because {dst_file} is larger")
    else:
        shutil.move(src_file, dst_file)
        logging.info(f"Moved file {src_file} to {dst_file}")

# Function to move folders with audio files to unknown album folder
def move_folders_with_audio_to_unknown():
    logging.info(f"Checking folders in music_download_folder: {music_download_folder}")

    for folder in os.listdir(music_download_folder):
        folder_path = os.path.join(music_download_folder, folder)

        if os.path.isdir(folder_path):
            logging.info(f"Processing folder: {folder_path}")

            delete_specific_files_in_all_subdirectories(folder_path)

            if not os.listdir(folder_path):
                logging.info(f"Folder {folder_path} is empty after deleting specific files. Removing folder.")
                shutil.rmtree(folder_path)
                continue

            contains_audio, contains_incomplete = folder_contains_audio_or_incomplete(folder_path)

            if contains_incomplete:
                logging.info(f"Skipping folder {folder_path} because it contains .incomplete files.")
                continue

            if contains_audio:
                destination_folder = None
                # Check for playlist marker
                if os.path.exists(os.path.join(folder_path, '.is_playlist')):
                    logging.info(f"Playlist folder detected: {folder}. Moving to destination root.")
                    destination_folder = os.path.join(destination_root, folder)
                    # Clean up marker file before moving
                    os.remove(os.path.join(folder_path, '.is_playlist'))
                else:
                    logging.info(f"Album/single detected: {folder}. Moving to unknown album folder for sorting.")
                    destination_folder = os.path.join(unknown_albums_dir, folder)

                try:
                    logging.info(f"Moving folder {folder_path} to {destination_folder}")
                    if os.path.exists(destination_folder):
                         logging.warning(f"Destination {destination_folder} already exists. Merging contents.")
                         shutil.copytree(folder_path, destination_folder, dirs_exist_ok=True)
                         shutil.rmtree(folder_path)
                    else:
                         shutil.move(folder_path, destination_folder)
                except (shutil.Error, OSError) as e:
                    logging.error(f"Error moving folder {folder_path} to {destination_folder}: {e}")
            else:
                logging.info(f"Skipping folder {folder_path} because it contains no audio files.")
        else:
            logging.info(f"Skipping non-directory item: {folder_path}")

# Function to update metadata using mutagen
def update_metadata(file_path, genre, album_artist):
    logging.debug(f"Updating metadata for {file_path}")
    try:
        if file_path.endswith('.mp3'):
            audio = EasyID3(file_path)
            audio['genre'] = genre
            audio['albumartist'] = album_artist
            audio.save()
        elif file_path.endswith('.flac'):
            audio = FLAC(file_path)
            audio['genre'] = genre
            audio['albumartist'] = album_artist
            audio.save()
        elif file_path.endswith('.m4a') or file_path.endswith('.mp4'):
            audio = MP4(file_path)
            audio['\xa9gen'] = genre
            audio['aART'] = album_artist
            audio.save()
        else:
            logging.warning(f"Unsupported file format: {file_path}")
            return False
        logging.info(f"Updated metadata for {file_path}: Genre = {genre}, Album Artist = {album_artist}")
        return True
    except Exception as e:
        logging.error(f"Error updating metadata for {file_path}: {e}")
        return False

# Function to move contents of one folder to another, merging if necessary
def move_folder_contents(src, dst, extensions=None):
    """
    Move contents of src to dst, merging if dst already exists.
    If `extensions` is provided, only files with those extensions are moved.
    Returns the number of newly added files.
    """
    new_files_count = 0
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.isdir(src_path):
            # Recursively merge directories and add to the count
            new_files_count += move_folder_contents(src_path, dst_path, extensions)
        else:
            # Check if the file is new before moving
            is_new_file = not os.path.exists(dst_path)
            
            # If extensions are specified, check the file extension
            if extensions:
                file_ext = os.path.splitext(item)[1].lower()
                if file_ext in extensions:
                    move_and_compare(src_path, dst_path)
                    if is_new_file:
                        new_files_count += 1
            else:
                # If no extensions are specified, move the file
                move_and_compare(src_path, dst_path)
                if is_new_file:
                    new_files_count += 1
    return new_files_count

# Function to process each artist folder
def process_artist_folder(artist_folder):
    artist_name = os.path.basename(artist_folder)
    logging.info(f"Processing artist folder: {artist_name}")

    # Recursively search for the artist in the destination root
    match_found = False
    for root, dirs, files in os.walk(destination_root):
        if artist_name in dirs:
            destination_artist_folder = os.path.join(root, artist_name)
            genre_folder = os.path.basename(os.path.dirname(destination_artist_folder))
            logging.info(f"Found matching artist folder: {destination_artist_folder} with Genre: {genre_folder}")
            match_found = True

            # Move all files and subfolders from source to destination
            for src_root, _, src_files in os.walk(artist_folder):
                dst_root = src_root.replace(artist_folder, destination_artist_folder, 1)
                os.makedirs(dst_root, exist_ok=True)

                # Set permissions for the destination folder
                set_permissions(dst_root)

                for file in src_files:
                    src_file = os.path.join(src_root, file)
                    dst_file = os.path.join(dst_root, file)

                    # Move and compare files based on size
                    move_and_compare(src_file, dst_file)

                    # Update metadata for supported formats
                    update_metadata(dst_file, genre_folder, artist_name)

            break

    if not match_found:
        # No matching artist folder found, move to new artists directory
        new_artist_folder = os.path.join(new_artists_dir, artist_name)
        logging.info(f"No match found, moving {artist_folder} to new artists directory: {new_artist_folder}")

        # If the new_artist_folder already exists, merge its contents
        if os.path.exists(new_artist_folder):
            logging.info(f"Destination folder {new_artist_folder} already exists. Merging contents.")
            move_folder_contents(artist_folder, new_artist_folder)
            # Remove the source folder after merging
            shutil.rmtree(artist_folder)
        else:
            shutil.move(artist_folder, new_artist_folder)

# Standalone function to delete specific files (.cue, .log, .m3u) in a directory and its subdirectories
def delete_specific_files_in_all_subdirectories(directory):
    logging.info(f"Deleting specific files (.cue, .log, .m3u) in: {directory}")
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(('.cue', '.log', '.m3u')):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    logging.info(f"Successfully deleted file: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting file {file_path}: {e}")

# Function to delete empty directories after all processing is complete
def cleanup_empty_directories(directory):
    logging.info(f"Deleting empty directories in: {directory}")
    for root, dirs, _ in os.walk(directory, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            if not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                    logging.info(f"Successfully deleted empty directory: {dir_path}")
                except Exception as e:
                    logging.error(f"Error deleting empty directory {dir_path}: {e}")

def create_m3u8_playlist(playlist_folder_path, playlist_name):
    """
    Creates an .m3u8 playlist file in the destination folder using actual files in the folder.
    This function will replace the existing playlist if it already exists.
    """
    music_extensions = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.wma', '.webm', '.opus', '.mka', '.mp4'}
    m3u8_file_path = os.path.join(playlist_folder_path, f"{playlist_name}.m3u8")
    
    # Get all music files from the destination folder (not from original source)
    song_files = []
    for root, dirs, files in os.walk(playlist_folder_path):
        for file in files:
            file_ext = os.path.splitext(file)[1].lower()
            if file_ext in music_extensions:
                # Use relative path from the playlist folder
                relative_path = os.path.relpath(os.path.join(root, file), playlist_folder_path)
                song_files.append(relative_path)
    
    if not song_files:
        logging.warning(f"No music files found in {playlist_folder_path} for playlist creation")
        return
    
    logging.info(f"Creating .m3u8 playlist file at: {m3u8_file_path} with {len(song_files)} tracks")
    
    try:
        # Remove existing playlist file if it exists (to replace it)
        if os.path.exists(m3u8_file_path):
            os.remove(m3u8_file_path)
            logging.info(f"Removed existing playlist file: {m3u8_file_path}")
        
        with open(m3u8_file_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            # Sort files alphabetically for consistent playlist order
            for song_file in sorted(song_files):
                f.write(f"{song_file}\n")
        
        logging.info(f"✓ Successfully created playlist file: {m3u8_file_path} with {len(song_files)} tracks")
        
    except Exception as e:
        logging.error(f"Failed to create .m3u8 playlist file for '{playlist_name}': {e}")

def move_playlist_folders():
    music_extensions = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.wma', '.webm', '.opus', '.mka'}
    playlist_extensions = {'.m3u', '.m3u8'}

    download_path_clean = str(download_path).strip('"').strip("'").rstrip('/')
    logging.info(f"Cleaned download_path: {repr(download_path_clean)}")

    if not os.path.exists(download_path_clean):
        logging.error(f"Download path does not exist: {download_path_clean}")
        return

    files_in_download_path = os.listdir(download_path_clean)
    logging.info(f"Checking for playlist folders in: {download_path_clean}, found {len(files_in_download_path)} items")    

    total_files_moved = 0
    playlists_to_process = []

    # --- First Pass: Move files and collect data ---
    for item in files_in_download_path:
        item_path = os.path.join(download_path_clean, item)
        if not os.path.isdir(item_path):
            continue

        try:
            all_files = os.listdir(item_path)
            # Check if folder contains playlist files
            playlist_files = [f for f in all_files if os.path.splitext(f)[1].lower() in playlist_extensions]
            music_files = [f for f in all_files if os.path.splitext(f)[1].lower() in music_extensions]
        except Exception as e:
            logging.error(f"Error reading directory {item_path}: {e}")
            continue

        # Only process folders that contain playlist files
        if playlist_files and music_files:
            playlist_name = item
            destination_folder = os.path.join(playlist_dir, playlist_name)
            logging.info(f"Found playlist folder '{playlist_name}' with {len(music_files)} songs and {len(playlist_files)} playlist files.")

            try:
                if os.path.exists(destination_folder):
                    logging.warning(f"Playlist folder {destination_folder} already exists. Merging music files.")
                
                newly_moved_count = move_folder_contents(item_path, destination_folder, music_extensions)

                logging.info(f"Moved {newly_moved_count} new music files to: {destination_folder}")
                total_files_moved += newly_moved_count
                playlists_to_process.append((playlist_name, destination_folder))

            except Exception as e:
                logging.error(f"Error moving music files from {item_path}: {e}")
        elif playlist_files and not music_files:
            logging.info(f"Found playlist folder '{item}' with only playlist files, no music files. Skipping.")
        elif not playlist_files and music_files:
            logging.info(f"Found folder '{item}' with music files but no playlist files. Skipping.")

    # --- Second Pass: Create playlists and cleanup ---
    playlists_created = 0
    if playlists_to_process:
        logging.info(f"Processing {len(playlists_to_process)} playlists...")
        for playlist_name, destination_folder in playlists_to_process:
            # Create the .m3u8 file using files in destination folder
            create_m3u8_playlist(destination_folder, playlist_name)
            playlists_created += 1
            
            # Clean up original folder if it still exists
            original_item_path = os.path.join(download_path_clean, playlist_name)
            if os.path.exists(original_item_path):
                try:
                    shutil.rmtree(original_item_path, ignore_errors=True)
                    logging.info(f"✓ Cleaned up leftover downloaded folder: {original_item_path}")
                except Exception as e:
                    logging.error(f"Failed to remove leftover folder {original_item_path}: {e}")
    
    if playlists_created > 0:
        logging.info(f"Successfully processed {playlists_created} playlists")
        # Trigger Navidrome scan after creating playlists
        trigger_navidrome_scan()
    else:
        logging.info("No playlists to process")
                    
# Main function to iterate through all artist folders in the source root
def main():
    logging.info(f"Starting the music sorting script with source_route: {source_route}")

    # Move playlist folders
    move_playlist_folders()