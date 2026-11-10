#!/usr/bin/env python3
"""One-shot Marstek cloud telemetry collector; runs on GitHub Actions.

Unofficial Marstek API; no writes to the physical station. No credentials,
upstream request URLs, account identifiers, or raw responses are logged.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

ORIGIN = 'https://eu.hamedata.com'
DESTINATION = 'https://svitlohelper-cloud.svitlohelper.workers.dev/ingest'


def fetch_json(method, path, args):
    url = ORIGIN + path + '?' + urllib.parse.urlencode(args)
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'SvitlohelperCollector/1.0',
    }, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(500001)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('marstek: http_' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('marstek: connection_failed') from None
    if len(raw) > 500000:
        raise RuntimeError('marstek: response_too_large')
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError('marstek: invalid_json') from None


def search_token(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() == 'token' and isinstance(v, str) and len(v) >= 8:
                return v
        for v in value.values():
            found = search_token(v, depth + 1)
            if found:
                return found
    if isinstance(value, list):
        for v in value:
            found = search_token(v, depth + 1)
            if found:
                return found
    return None


def valid_number(raw):
    if raw is None or isinstance(raw, bool) or raw == '':
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (float('-inf') < n < float('inf')):
        return None
    return n


def find_readings(value, depth=0):
    if depth > 10:
        return []
    found = []
    if isinstance(value, dict):
        fields = {str(k).lower(): v for k, v in value.items()}
        soc = valid_number(fields.get('soc'))
        if soc is not None and 0 <= soc <= 100 and ('charge' in fields or 'discharge' in fields):
            found.append(fields)
        for v in value.values():
            found.extend(find_readings(v, depth + 1))
    if isinstance(value, list):
        for v in value:
            found.extend(find_readings(v, depth + 1))
    return found


def parse_report_time(raw):
    """Only timestamps with explicit timezone or epoch numbers are trusted."""
    if isinstance(raw, (int, float)):
        v = float(raw)
        return int(v / 1000 if v > 1e12 else v)
    if not isinstance(raw, str):
        return None
    if raw.isdigit() and len(raw) in (10, 13):
        v = int(raw)
        return v // 1000 if len(raw) == 13 else v
    if ('T' in raw and (raw.endswith('Z') or raw[-6:-5] in ('+', '-'))):
        try:
            return int(datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp())
        except ValueError:
            return None
    return None


def collect(email, pwd_md5):
    login = fetch_json('POST', '/app/Solar/v2_get_device.php', {
        'mailbox': email, 'pwd': pwd_md5,
    })
    token = search_token(login)
    if not token:
        raise RuntimeError('marstek: login_token_missing')
    devices = fetch_json('GET', '/ems/api/v1/getDeviceList', {'token': token})
    reading_list = [r for r in find_readings(devices) if str(r.get('model', '')).upper() != 'HME-3']
    if not reading_list:
        raise RuntimeError('marstek: readings_missing')
    r = reading_list[0]
    soc = valid_number(r.get('soc'))
    charge = valid_number(r.get('charge'))
    discharge = valid_number(r.get('discharge'))
    if charge is not None and not (0 <= charge <= 12000):
        charge = None
    if discharge is not None and not (0 <= discharge <= 12000):
        discharge = None
    if charge is None and discharge is None:
        raise RuntimeError('marstek: power_invalid')
    load = valid_number(r.get('load'))
    if load is not None and not (0 <= load <= 12000):
        load = None
    timestamp = parse_report_time(r.get('report_time'))
    return {
        'soc': soc,
        'charge_power_w': charge,
        'discharge_power_w': discharge,
        'load_power_w': load,
        'device_updated_at_epoch': timestamp,
        'fetched_at_epoch': int(time.time()),
    }


def send_status(status, ingest_key):
    body = json.dumps(status, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(DESTINATION, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'X-Ingest-Key': ingest_key,
        'User-Agent': 'SvitlohelperCollector/1.0',
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError('cloudflare: ingest_failed')
            result = json.loads(response.read(4096))
            if not (result.get('ok') and result.get('stored')):
                raise RuntimeError('cloudflare: ingest_failed')
    except urllib.error.HTTPError as exc:
        raise RuntimeError('cloudflare: http_' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('cloudflare: connection_failed') from None


def main():
    email = os.environ.get('MARSTEK_EMAIL', '').strip()
    md5 = os.environ.get('MARSTEK_PWD_MD5', '').strip()
    key = os.environ.get('INGEST_KEY', '').strip()
    if not email or not key or len(key) < 32 or len(md5) != 32:
        raise RuntimeError('config: missing_secrets')
    try:
        int(md5, 16)
    except ValueError:
        raise RuntimeError('config: invalid_password_hash') from None
    status = collect(email, md5)
    send_status(status, key)
    print('Marstek: snapshot accepted by Cloudflare; battery data available (values withheld).')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        # Only safe, self-authored failure codes; no secrets in logs.
        print('Collector failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
