import pytest
import requests
import time
import os

# --- API Configuration ---
JELLYFIN_URL = "http://localhost:8096"
JELLYFIN_API_KEY = "26d7fae3698e4b5496b5e0f28e821d98"

NAVIDROME_URL = "http://localhost:4533"
NAVIDROME_USER = "admin"
NAVIDROME_PASS = "admin"

# --- Fixtures ---

@pytest.fixture(scope="session")
def jellyfin_user_id():
    """Fetches the first available Jellyfin User ID to be used in tests."""
    try:
        headers = {"X-Emby-Token": JELLYFIN_API_KEY}
        response = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=5)
        response.raise_for_status()
        users = response.json()
        if not users:
            pytest.fail("No users found on the Jellyfin server.")
        return users[0]["Id"]
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Could not connect to Jellyfin to get User ID: {e}")

@pytest.fixture(scope="session")
def navidrome_auth_token():
    """Logs into Navidrome and retrieves a session token."""
    try:
        user = NAVIDROME_USER
        password = NAVIDROME_PASS
        params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password}
        response = requests.get(
            f"{NAVIDROME_URL}/rest/ping",
            auth=(user, password),
            params=params,
            timeout=5
        )
        response.raise_for_status()
        return (NAVIDROME_USER, NAVIDROME_PASS)
    except requests.exceptions.RequestException as e:
        pytest.fail(f"Could not connect to Navidrome to get auth token: {e}")

@pytest.fixture
def unique_playlist_name():
    """Generates a unique playlist name using a timestamp."""
    return f"Test Playlist {int(time.time() * 1000)}"

@pytest.fixture
def created_jellyfin_playlist(jellyfin_user_id, unique_playlist_name):
    """Fixture to create and automatically clean up a Jellyfin playlist."""
    headers = {"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"}
    create_payload = {"Name": unique_playlist_name, "UserId": jellyfin_user_id}
    
    response = requests.post(f"{JELLYFIN_URL}/Playlists", headers=headers, json=create_payload)
    response.raise_for_status()
    playlist_data = response.json()
    playlist_id = playlist_data["Id"]
    
    yield playlist_id, unique_playlist_name
    
    requests.delete(f"{JELLYFIN_URL}/Items/{playlist_id}", headers=headers)

@pytest.fixture
def created_navidrome_playlist(navidrome_auth_token, unique_playlist_name):
    """Fixture to create and automatically clean up a Navidrome playlist."""
    user, password = navidrome_auth_token
    
    # Pre-cleanup: Ensure no playlist with this name exists from a failed previous run
    params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password}
    all_playlists_response = requests.get(f"{NAVIDROME_URL}/rest/getPlaylists", auth=(user, password), params=params)
    if all_playlists_response.status_code == 200:
        playlists_data = all_playlists_response.json().get("subsonic-response", {}).get("playlists", {})
        playlists = playlists_data.get("playlist", []) if playlists_data else []
        # Handle case where playlist is a single item (not a list)
        if isinstance(playlists, dict):
            playlists = [playlists]
        for p in playlists:
            if p.get('name') == unique_playlist_name:
                delete_params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password, 'id': p['id']}
                requests.post(f"{NAVIDROME_URL}/rest/deletePlaylist", auth=(user, password), params=delete_params)

    # SETUP: Create the playlist
    params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password, 'name': unique_playlist_name}
    response = requests.post(f"{NAVIDROME_URL}/rest/createPlaylist", auth=(user, password), params=params)
    response.raise_for_status()
    playlist_data = response.json()
    
    subsonic_response = playlist_data.get("subsonic-response", {})
    
    # Check for errors first
    if "error" in subsonic_response:
        error_code = subsonic_response["error"]["code"]
        error_message = subsonic_response["error"].get("message", "Unknown error")
        pytest.fail(f"Navidrome returned error {error_code}: {error_message}")
    
    # Handle Navidrome's response structure
    playlist_info = subsonic_response.get("playlist")
    if not playlist_info:
        pytest.fail("Navidrome did not return playlist info after creation.")
    
    # Handle case where playlist_info might be a dict or a list
    if isinstance(playlist_info, list):
        if not playlist_info:
            pytest.fail("Navidrome returned empty playlist list.")
        playlist_id = playlist_info[0]["id"]
    else:
        playlist_id = playlist_info["id"]

    yield playlist_id, unique_playlist_name

    # TEARDOWN: Delete the playlist
    delete_params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password, 'id': playlist_id}
    requests.post(f"{NAVIDROME_URL}/rest/deletePlaylist", auth=(user, password), params=delete_params)

# --- Jellyfin API Tests ---

def test_jellyfin_create_and_delete_playlist(jellyfin_user_id, unique_playlist_name):
    headers = {"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"}
    create_payload = {"Name": unique_playlist_name, "UserId": jellyfin_user_id}
    create_response = requests.post(f"{JELLYFIN_URL}/Playlists", headers=headers, json=create_payload)
    assert create_response.status_code == 200
    playlist_data = create_response.json()
    assert "Id" in playlist_data
    playlist_id = playlist_data["Id"]
    delete_response = requests.delete(f"{JELLYFIN_URL}/Items/{playlist_id}", headers=headers)
    assert delete_response.status_code == 204

def test_jellyfin_search_existing_playlist(jellyfin_user_id, created_jellyfin_playlist):
    playlist_id, playlist_name = created_jellyfin_playlist
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    params = {"searchTerm": playlist_name, "IncludeItemTypes": "Playlist", "Recursive": "true", "UserId": jellyfin_user_id}
    response = requests.get(f"{JELLYFIN_URL}/Items", headers=headers, params=params)
    assert response.status_code == 200
    results = response.json()
    assert results["TotalRecordCount"] == 1
    assert results["Items"][0]["Id"] == playlist_id

def test_jellyfin_search_non_existing_playlist(jellyfin_user_id):
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    non_existent_name = f"non-existent-{int(time.time() * 1000)}"
    params = {"searchTerm": non_existent_name, "IncludeItemTypes": "Playlist", "Recursive": "true", "UserId": jellyfin_user_id}
    response = requests.get(f"{JELLYFIN_URL}/Items", headers=headers, params=params)
    assert response.status_code == 200
    assert response.json()["TotalRecordCount"] == 0

# --- Navidrome API Tests (using Subsonic API) ---

def test_navidrome_create_and_delete_playlist(navidrome_auth_token, unique_playlist_name):
    user, password = navidrome_auth_token
    params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password, 'name': unique_playlist_name}
    create_response = requests.post(f"{NAVIDROME_URL}/rest/createPlaylist", auth=(user, password), params=params)
    assert create_response.status_code == 200
    
    response_json = create_response.json().get("subsonic-response", {})
    
    # Check for errors
    if "error" in response_json:
        error_code = response_json["error"]["code"]
        # Error code 10 means "Required parameter is missing"
        # This might happen if the playlist creation failed due to missing params
        if error_code == 10:
            pytest.fail(f"Required parameter missing: {response_json['error'].get('message', 'Unknown')}")
        # If it's some other error, handle accordingly
        else:
            pytest.fail(f"Unexpected error {error_code}: {response_json['error'].get('message', 'Unknown')}")
    
    # Get playlist info from successful creation
    playlist_data = response_json.get("playlist")
    if not playlist_data:
        pytest.fail("No playlist data returned after creation")
    
    # Handle response format (might be list or dict)
    if isinstance(playlist_data, list):
        if not playlist_data:
            pytest.fail("Empty playlist list returned")
        playlist_info = playlist_data[0]
    else:
        playlist_info = playlist_data
    
    assert playlist_info["name"] == unique_playlist_name
    playlist_id = playlist_info["id"]

    # Delete the playlist
    delete_params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password, 'id': playlist_id}
    delete_response = requests.post(f"{NAVIDROME_URL}/rest/deletePlaylist", auth=(user, password), params=delete_params)
    assert delete_response.status_code == 200

def test_navidrome_search_existing_playlist(navidrome_auth_token, created_navidrome_playlist):
    user, password = navidrome_auth_token
    playlist_id, playlist_name = created_navidrome_playlist
    params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password}
    response = requests.get(f"{NAVIDROME_URL}/rest/getPlaylists", auth=(user, password), params=params)
    assert response.status_code == 200
    
    playlists_data = response.json()["subsonic-response"]["playlists"]
    all_playlists = playlists_data.get("playlist", [])
    
    # Handle case where playlist might be a single dict instead of a list
    if isinstance(all_playlists, dict):
        all_playlists = [all_playlists]
    
    found_playlist = next((p for p in all_playlists if p["id"] == playlist_id), None)
    assert found_playlist is not None
    assert found_playlist["name"] == playlist_name

def test_navidrome_search_non_existing_playlist(navidrome_auth_token):
    user, password = navidrome_auth_token
    non_existent_name = f"non-existent-{int(time.time() * 1000)}"
    params = {'f': 'json', 'v': '1.16.1', 'c': 'pytest', 'u': user, 'p': password}
    response = requests.get(f"{NAVIDROME_URL}/rest/getPlaylists", auth=(user, password), params=params)
    assert response.status_code == 200
    
    playlists_data = response.json().get("subsonic-response", {}).get("playlists", {})
    all_playlists = playlists_data.get("playlist", []) if playlists_data else []
    
    # Handle case where playlist might be a single dict instead of a list
    if isinstance(all_playlists, dict):
        all_playlists = [all_playlists]
    
    found_playlist = next((p for p in all_playlists if p["name"] == non_existent_name), None)
    assert found_playlist is None