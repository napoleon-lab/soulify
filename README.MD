See the upstream for details https://github.com/WB2024/soulify

## Docker Deployment

The easiest way to get Soulify running is with Docker.

To override a setting, create an environment variable with the prefix `SOULIFY_`, followed by the section and key in uppercase. For example, to override `base_url` in the `[Server]` section, you would set the environment variable `SOULIFY_SERVER_BASE_URL`.

### Using Docker Compose (Recommended)

The included `docker-compose.yml` file is a great way to test the application. It sets up both the Soulify application and a Jellyfin server for a complete media experience.

1.  **Configure `.config.ini`**: Before starting, make sure to fill out your details in the `config-sample.ini` file.
2.  **Run Docker Compose**:
    ```bash
    docker-compose up -d
    ```

### Using the Dockerfile (Standalone)

If you already have a Jellyfin setup or prefer to run Soulify by itself, you can use the `Dockerfile` to build and run the container independently.

1.  **Build the image**:
    ```bash
    docker build -t soulify .
    ```
2.  **Run the container**:
    ```bash
    docker run -d -p 5000:5000 \
      -v ./music/downloads:/app/downloads \
      -v ./music/library:/app/music \
      -v ./.config.ini:/app/config.ini \
      --name soulify-container \
      soulify
    ```

### Docker

When running with Docker, you must mount your `.config.ini` file as a volume:

```bash
docker run -v $(pwd)/.config.ini:/app/config.ini ...
```

### Spotify HTTPS Requirement & Tailscale

Spotify's API requires that the redirect URI for authentication be an `https` URL. This can be tricky for a local server. A simple solution is to use [Tailscale](https://tailscale.com/), which can provide a secure HTTPS URL for your machine.

1.  **Install Tailscale** on your server.
2.  **Get a certificate** for your machine:
    ```bash
    tailscale cert "your-tailscale-machine-name.your-domain.ts.net"
    ```
    This will generate a certificate (`.crt`) and a key (`.key`) file.
3.  **Update `config.ini`** with the paths to these files under the `[Server]` section:
    ```ini
    [Server]
    ssl_cert_path = /path/to/your-tailscale-machine-name.your-domain.ts.net.crt
    ssl_key_path = /path/to/your-tailscale-machine-name.your-domain.ts.net.key
    ```
4.  **Set your Spotify Redirect URI** in the Spotify Developer Dashboard and in `config.ini` to:
    `https://your-tailscale-machine-name.your-domain.ts.net:5000/callback`
