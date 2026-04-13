# Emeet Camera Daemon

This allows you to use your camera without running Emeetstream and introducing additional latency or bugs when moving through access points. 
Connects directly to Emeet cameras over their SSL control channel, configures RIST video streaming to a destination machine, and automatically reconnects whenever a camera drops or roams between WiFi access points.

You will need to install Emeetstream at-least once on a mac to use this (to generate the certificate).

## How it works

The camera exposes a TLS control channel on port 2023. On connect, the daemon authenticates and sends a `set_stream_resolution` command that tells the camera to push its video stream via RIST UDP to a destination IP and port. The camera stores this configuration in RAM — it resets on every WiFi reconnect. The daemon detects drops and immediately re-applies the configuration when the camera comes back.

## Requirements

- Python 3.8+
- No external dependencies (uses stdlib only)
- `client.pem` — TLS client certificate, extracted from Emeetstream (not in repo — see below)

## Setup

### 1. Extract the client certificate

`client.pem` is not in the repo. Extract it from Emeetstream's app bundle:

```bash
openssl pkcs12 -legacy -nodes \
  -in /Applications/EMEETSTREAM.app/Contents/Resources/clientkey.p12 \
  -passin pass:client \
  -out client.pem
```

### 2. Configure

Edit `config.json`:

```json
{
  "cameras": [
    { "ip": "192.168.1.171", "name": "Camera 1", "rist_port": 62201 },
    { "ip": "192.168.1.172", "name": "Camera 2", "rist_port": 62202 },
    { "ip": "192.168.1.173", "name": "Camera 3", "rist_port": 62203 }
  ],
  "destination_ip": "192.168.1.10"
}
```

- `cameras` — list of cameras with their IPs and the RIST port each will stream to
- `destination_ip` — the machine that will receive the streams (where OBS is running)

### 2. Run

```bash
python3 daemon.py
```

Or with a custom config path:

```bash
python3 daemon.py --config /path/to/config.json
```

### 3. Receive in OBS

On the destination machine, add a **Media Source** for each camera:

| Camera   | OBS URL                              |
|----------|--------------------------------------|
| Camera 1 | `rist://@0.0.0.0:62201?buffer=20`   |
| Camera 2 | `rist://@0.0.0.0:62202?buffer=20`   |
| Camera 3 | `rist://@0.0.0.0:62203?buffer=20`   |

<img width="752" height="634" alt="image" src="https://github.com/user-attachments/assets/23f461e1-bec6-4d51-aae8-a72718beca52" />

## Configuration reference

| Key | Description | Default |
|-----|-------------|---------|
| `destination_ip` | IP of the machine receiving RIST streams | — |
| `camera_port` | Camera control port | `2023` |
| `client_cert` | Path to TLS client cert (relative to script) | `client.pem` |
| `reconnect_delay_s` | Seconds between reconnect attempts | `0.5` |
| `heartbeat_interval_s` | Seconds between heartbeats | `5` |
| `stream.video_bitrate` | Video bitrate in kbps | `10000` |
| `stream.video_width` | Video width in pixels | `1920` |
| `stream.video_height` | Video height in pixels | `1080` |
| `stream.video_framerate` | Frames per second | `60` |
| `stream.audio_bitrate` | Audio bitrate in kbps | `128` |
| `stream.audio_channels` | Audio channels (1 = mono, 2 = stereo) | `1` |
| `rist.buffer_ms` | RIST receive buffer in milliseconds (used in OBS URL) | `20` |

## Router setup

Give each camera a DHCP reservation based on its MAC address so the IP never changes when roaming between access points:

| Camera | MAC | IP |
|--------|-----|----|
| Camera 1 | `aa:bb:cc:dd:ee:ff` | `192.168.1.171` |

## Notes

- Only one device can hold the camera's control connection at a time. Quit Emeetstream before running this daemon.
- The daemon does not need to run on the same machine as OBS. It just needs network access to the cameras.
- The `client.pem` certificate was extracted from Emeetstream's app bundle (`clientkey.p12`, password `client`). It is not secret — the camera does not enforce mutual TLS.
