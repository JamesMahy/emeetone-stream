#!/usr/bin/env python3
"""
emeet daemon — persistent camera connection manager.

Reads config.json from the same directory. Manages all configured cameras
in parallel. On each connect, authenticates with the camera and configures
the RIST stream destination. Reconnects automatically on drop.

Usage:
  python3 daemon.py [--config path/to/config.json]
"""

import ssl, socket, time, json, struct, sys, threading, os, argparse

# ── load config ───────────────────────────────────────────────────────────────
def load_config(path):
    with open(path) as f:
        return json.load(f)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

parser = argparse.ArgumentParser()
parser.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.json'))
args = parser.parse_args()

cfg = load_config(args.config)

CAMERA_PORT         = cfg['camera_port']
RECONNECT_DELAY     = cfg['reconnect_delay_s']
HEARTBEAT_INTERVAL  = cfg['heartbeat_interval_s']
CLIENT_PEM          = os.path.join(SCRIPT_DIR, cfg['client_cert'])
MAC_UUID            = cfg.get('client_uuid', str(__import__('uuid').uuid4()).upper())

STREAM_PARAMS = {
    'audioBitrate':      cfg['stream']['audio_bitrate'],
    'protocol':          1,
    'videoHeight':       cfg['stream']['video_height'],
    'videoWidth':        cfg['stream']['video_width'],
    'type':              2,
    'videoBitrate':      cfg['stream']['video_bitrate'],
    'audioChannelCount': cfg['stream']['audio_channels'],
    'videoFrameRate':    cfg['stream']['video_framerate'],
}

# ── helpers ───────────────────────────────────────────────────────────────────
def ts():
    return time.strftime('%H:%M:%S')

def log(camera_name, msg):
    print(f'[{ts()}] [{camera_name}] {msg}', flush=True)

def frame(msg_dict, type_byte=0x00):
    body = json.dumps(msg_dict, separators=(',', ':')).encode()
    return struct.pack('<I', len(body)) + bytes([type_byte]) + body

# ── connection session ────────────────────────────────────────────────────────
class CameraSession:
    def __init__(self, ssl_sock, rist_url, name):
        self.ssl      = ssl_sock
        self.rist_url = rist_url
        self.name     = name
        self._lock    = threading.Lock()
        self._alive   = True
        self._msg_q   = []

    def log(self, msg):
        log(self.name, msg)

    def close(self):
        self._alive = False
        try:
            self.ssl.close()
        except:
            pass

    def send(self, msg_dict, type_byte=0x00):
        with self._lock:
            self.ssl.send(frame(msg_dict, type_byte))

    def _recv_exactly(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.ssl.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('camera closed connection')
            buf += chunk
        return buf

    def recv_msg(self):
        hdr    = self._recv_exactly(5)
        length = struct.unpack('<I', hdr[:4])[0]
        body   = self._recv_exactly(length)
        return json.loads(body)

    def reader_loop(self):
        self.ssl.settimeout(15)
        while self._alive:
            try:
                msg  = self.recv_msg()
                head = msg.get('head', {})
                name = head.get('msg', '?')
                if name not in ('status_changed_notice', 'heartbeat_resp'):
                    self.log(f'← {name}')
                self._msg_q.append(msg)
            except socket.timeout:
                self.log('No data for 15s — connection dead')
                self._alive = False
                break
            except Exception as e:
                if self._alive:
                    self.log(f'Reader error: {e}')
                self._alive = False
                break

    def configure(self):
        self.send({'head': {
            'fromId':   MAC_UUID,
            'os':       'macOS',
            'msgValue': 11002,
            'timezone': 'GMT+00:00',
            'msg':      'check_device',
            'tag':      'EMEET Multi-App',
            'type':     2,
        }}, type_byte=0x01)
        self.log('→ check_device')

        deadline = time.time() + 8
        while time.time() < deadline and self._alive:
            for m in list(self._msg_q):
                if m.get('head', {}).get('msg') == 'check_device_resp':
                    self._msg_q.remove(m)
                    self.log(f'← check_device_resp  code={m["head"].get("code")}')
                    break
            else:
                time.sleep(0.1)
                continue
            break

        if not self._alive:
            return False

        self.send({'head': {
            **STREAM_PARAMS,
            'ristUrl':  self.rist_url,
            'msgValue': 12002,
            'msg':      'set_stream_resolution',
            'fromId':   MAC_UUID,
        }}, type_byte=0x00)
        self.log(f'→ set_stream_resolution  ristUrl={self.rist_url}')
        return True

    def heartbeat_loop(self):
        type_bytes = [0x00, 0x09]
        tick = 0
        while self._alive:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._alive:
                break
            try:
                self.send({'head': {
                    'msg':      'heartbeat',
                    'type':     2,
                    'msgValue': 11010,
                    'fromId':   MAC_UUID,
                }}, type_byte=type_bytes[tick % 2])
                tick += 1
            except Exception as e:
                self.log(f'Heartbeat failed: {e}')
                self._alive = False
                break

# ── TLS context ───────────────────────────────────────────────────────────────
def make_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        ctx.load_cert_chain(CLIENT_PEM)
    except Exception as e:
        print(f'[warn] Client cert not loaded: {e}')
    return ctx

CTX = make_ctx()

# ── per-camera reconnect loop ─────────────────────────────────────────────────
def run_camera(camera_cfg, dest_ip):
    camera_ip   = camera_cfg['ip']
    name        = camera_cfg['name']
    rist_port   = camera_cfg['rist_port']
    rist_url    = f'rist://{dest_ip}:{rist_port}'

    log(name, f'Starting — camera {camera_ip}:{CAMERA_PORT} → {rist_url}')

    attempt = 0
    while True:
        attempt += 1
        log(name, f'Connecting (attempt {attempt})...')
        try:
            raw  = socket.create_connection((camera_ip, CAMERA_PORT), timeout=6)
            sock = CTX.wrap_socket(raw)
            log(name, f'Connected ({sock.version()})')

            sess = CameraSession(sock, rist_url, name)

            threading.Thread(target=sess.reader_loop, daemon=True).start()

            if not sess.configure():
                sess.close()
                time.sleep(RECONNECT_DELAY)
                continue

            log(name, 'RIST configured — maintaining connection')
            threading.Thread(target=sess.heartbeat_loop, daemon=True).start()

            while sess._alive:
                time.sleep(0.5)

            log(name, 'Connection lost')
            sess.close()

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(name, f'Connect failed: {e}')

        log(name, f'Retrying in {RECONNECT_DELAY}s...')
        try:
            time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            break

# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    cameras  = cfg['cameras']
    dest_ip  = cfg['destination_ip']
    buf_ms   = cfg['rist']['buffer_ms']

    print(f'Emeet camera daemon')
    print(f'Destination: {dest_ip}')
    print(f'Cameras:')
    for c in cameras:
        print(f'  {c["name"]} ({c["ip"]}) → rist://@0.0.0.0:{c["rist_port"]}?buffer={buf_ms}')
    print()

    if len(cameras) == 1:
        run_camera(cameras[0], dest_ip)
    else:
        threads = [
            threading.Thread(
                target=run_camera,
                args=(c, dest_ip),
                name=c['name'],
                daemon=True,
            )
            for c in cameras
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            print('\nStopping.')
