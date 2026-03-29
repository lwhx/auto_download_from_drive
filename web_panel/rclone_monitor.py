import requests
import json
import os

def get_rclone_progress(rc_port):
    try:
        resp = requests.post(f'http://127.0.0.1:{rc_port}/core/stats', timeout=3)
        data = resp.json()

        transferring = data.get('transferring', [])
        current_file_info = transferring[0] if transferring else {}

        # Prefer file-level fields from transferring[0]; fall back to global stats
        current_file = current_file_info.get('name', '')
        percentage = current_file_info.get('percentage', 0)
        file_bytes = current_file_info.get('bytes', data.get('bytes', 0))
        file_size = current_file_info.get('size', 0)
        # File-level eta is more accurate than global eta
        eta = current_file_info.get('eta', data.get('eta'))
        # Use global speed (instantaneous) as file-level speed can be 0 at start
        speed = data.get('speed', current_file_info.get('speed', 0))

        return {
            'bytes': file_bytes,
            'totalBytes': file_size,
            'speed': speed,
            'speedMBps': round(speed / 1024 / 1024, 2),
            'eta': eta,
            'percentage': round(percentage, 2),
            'current_file': current_file
        }
    except:
        return None

def get_all_transfers_progress(transfers_path):
    if not os.path.exists(transfers_path):
        return {}

    try:
        with open(transfers_path, 'r') as f:
            transfers = json.load(f)

        if not transfers:
            return {}

        result = {}
        for key, transfer in transfers.items():
            rc_port = transfer.get('rc_port')
            if rc_port:
                progress = get_rclone_progress(rc_port)
                if progress:
                    result[key] = {
                        'transfer_info': transfer,
                        'progress': progress
                    }
                else:
                    # RC not ready yet — still show the transfer with a placeholder
                    result[key] = {
                        'transfer_info': transfer,
                        'progress': {
                            'bytes': 0,
                            'totalBytes': 0,
                            'speed': 0,
                            'speedMBps': 0,
                            'eta': None,
                            'percentage': 0,
                            'current_file': transfer.get('source_file', ''),
                            'connecting': True
                        }
                    }

        return result
    except:
        return {}
