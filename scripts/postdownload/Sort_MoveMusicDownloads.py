import os
import shutil
import logging
import subprocess
import requests  # For making API calls
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
import configparser

# Setup logging
script_dir = os.path.dirname(os.path.realpath(__file__))
log_file = os.path.join(script_dir, "music_sorting_debug.log")
logging.basicConfig(filename=log_file, level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Path to the config files (2 folder levels above the script)
config_dir = os.path.abspath(os.path.join(script_dir, "../../"))
pdscript_conf_path = os.path.join(config_dir, 'pdscript.conf')

# Function to read configurations for pdscript.conf
def load_pdscript_config():
    config_pdscript = configparser.ConfigParser()
    config_pdscript.read(pdscript_conf_path)

    Source_route = config_pdscript.get('Paths', 'Source_route')
    destination_root = config_pdscript.get('Paths', 'destination_root')
    new_artists_dir = config_pdscript.get('Paths', 'new_artists_dir')
    music_download_folder = config_pdscript.get('Paths', 'music_download_folder')
    unknown_albums_dir = config_pdscript.get('Paths', 'unknown_albums_dir')

    API_BASE_URL = config_pdscript.get('API Details', 'API_BASE_URL')
    API_AUTH_TOKEN = config_pdscript.get('API Details', 'API_AUTH_TOKEN')

    logging.debug(f"pdscript.conf contents: Source_route={Source_route}, destination_root={destination_root}, "
                  f"new_artists_dir={new_artists_dir}, music_download_folder={music_download_folder}, "
                  f"unknown_albums_dir={unknown_albums_dir}, API_BASE_URL={API_BASE_URL}")

    return Source_route, destination_root, new_artists_dir, music_download_folder, unknown_albums_dir, API_BASE_URL, API_AUTH_TOKEN

# Load configuration variables
Source_route, destination_root, new_artists_dir, music_download_folder, unknown_album_folder, API_BASE_URL, API_AUTH_TOKEN = load_pdscript_config()

# Audio file extensions to check
audio_extensions = {'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.wav', '.ogg', '.wma', '.alac', '.aiff', '.opus'}

# API headers
HEADERS = {
    'Authorization': f'MediaBrowser Token={API_AUTH_TOKEN}',
    'accept': 'application/json',
    'Content-Type': 'application/json'
}

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

            # Delete specific files (.cue, .log, .m3u) in the folder
            delete_specific_files_in_all_subdirectories(folder_path)

            # Check if the folder is empty after deleting unwanted files
            if not os.listdir(folder_path):
                logging.info(f"Folder {folder_path} is empty after deleting specific files. Removing folder.")
                shutil.rmtree(folder_path)
                continue

            # Recheck after deleting specific files
            contains_audio, contains_incomplete = folder_contains_audio_or_incomplete(folder_path)

            if contains_incomplete:
                logging.info(f"Skipping folder {folder_path} because it contains .incomplete files.")
                continue

            if contains_audio:
                # Move folder to Unknown Album Folder
                destination_folder = os.path.join(unknown_album_folder, folder)
                try:
                    logging.info(f"Moving folder {folder_path} to {destination_folder}")
                    shutil.move(folder_path, destination_folder)
                except shutil.Error as e:
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

# Function to make GET request and fetch artist details
def fetch_artist_info(artist_name):
    logging.debug(f"Fetching artist info for {artist_name}")
    try:
        url = f"{API_BASE_URL}/Artists/{artist_name}"
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 200:
            artist_info = response.json()
            logging.info(f"Successfully fetched artist info for {artist_name}")
            return artist_info
        else:
            logging.error(f"Failed to fetch artist info for {artist_name}: Status {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Error fetching artist info for {artist_name}: {e}")
        return None

# Function to refresh artist metadata using the artist ID
def refresh_artist_metadata(artist_id):
    logging.debug(f"Refreshing metadata for artist ID {artist_id}")
    try:
        url = f"{API_BASE_URL}/Items/{artist_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default&ReplaceAllImages=false&ReplaceAllMetadata=false"
        response = requests.post(url, headers=HEADERS)
        if response.status_code == 204:
            logging.info(f"Successfully refreshed metadata for artist ID {artist_id}")
        else:
            logging.error(f"Failed to refresh metadata for artist ID {artist_id}: Status {response.status_code}")
    except Exception as e:
        logging.error(f"Error refreshing metadata for artist ID {artist_id}: {e}")

# Function to move contents of one folder to another, merging if necessary
def move_folder_contents(src, dst):
    """Move contents of src to dst, merging if dst already exists."""
    if not os.path.exists(dst):
        os.makedirs(dst)

    for item in os.listdir(src):
        src_path = os.path.join(src, item)
        dst_path = os.path.join(dst, item)

        if os.path.isdir(src_path):
            # Recursively merge directories
            move_folder_contents(src_path, dst_path)
        else:
            # Move files, but replace if the file in dst is smaller
            move_and_compare(src_path, dst_path)

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

            # Make API calls to fetch artist info and refresh metadata
            artist_info = fetch_artist_info(artist_name)
            if artist_info and 'Id' in artist_info:
                refresh_artist_metadata(artist_info['Id'])

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

# Main function to iterate through all artist folders in the source root
def main():
    logging.info(f"Starting the music sorting script with Source_route: {Source_route}")

    # Delete specific files (.cue, .log, .m3u) in relevant directories before any other processing
    delete_specific_files_in_all_subdirectories(Source_route)
    delete_specific_files_in_all_subdirectories(music_download_folder)
    delete_specific_files_in_all_subdirectories(unknown_album_folder)

    # Make sure the new artist directory exists
    if not os.path.exists(new_artists_dir):
        os.makedirs(new_artists_dir)
        logging.info(f"Created new artists directory: {new_artists_dir}")
    else:
        logging.info(f"New artists directory already exists: {new_artists_dir}")

    # Log all items in Source_route
    items = os.listdir(Source_route)
    logging.info(f"Items in Source_route: {items}")

    # Process each artist folder in Source_route
    for item in items:
        artist_folder = os.path.join(Source_route, item)
        logging.info(f"Processing item: {item}")

        if os.path.isdir(artist_folder):
            logging.info(f"Found artist folder: {artist_folder}, starting process_artist_folder")
            process_artist_folder(artist_folder)
        else:
            logging.info(f"Skipping non-directory item: {artist_folder}")

    # After processing artist folders, move folders with audio files to unknown album folder
    move_folders_with_audio_to_unknown()

    # Run the empty directory cleanup after processing everything else at source
    cleanup_empty_directories(Source_route)

    # Run the empty directory cleanup after processing everything else at download folder
    cleanup_empty_directories(music_download_folder)
    
    # Run the empty directory cleanup after processing everything else at unknown album folder
    cleanup_empty_directories(unknown_album_folder)

if __name__ == "__main__":
    main()

