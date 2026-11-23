"""Cloud-based seat reader using curl_cffi for TLS compatibility."""
import json
import re
from curl_cffi import requests as cffi_requests
from api_source import seat_layout_url, ReadError, retry_delay

def read(show, manual=False):
    """
    Reads the seat layout for a given show.
    Returns a dict with 'rows', 'seats', 'available', and 'url'.
    """
    try:
        url = seat_layout_url(show)
    except Exception as e:
        raise ReadError(f"Invalid show identity: {e}")

    try:
        # Use curl_cffi with Safari fingerprint to avoid Cloudflare 403s
        response = cffi_requests.get(
            url, 
            impersonate="safari", 
            timeout=25,
            allow_redirects=True
        )
        
        # Handle rate limits and HTTP errors
        if response.status_code == 429:
            retry = retry_delay(429, response.headers.get('Retry-After'))
            raise ReadError('Source HTTP 429', 429, retry)
        elif response.status_code >= 400:
            raise ReadError(f'Source HTTP {response.status_code}', response.status_code, 180)
            
        html = response.text
        
        # BookMyShow embeds the layout in window.__INITIAL_STATE__
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', html)
        if not match:
            raise ReadError('Could not find seat layout data in response')

        data = json.loads(match.group(1))
        
        # Extract the seat map from the JSON
        seat_data = data.get('seatLayout', {}).get('data', {})
        if not seat_data:
            seat_data = data.get('showDetails', {}).get('seatLayout', {})

        if not seat_data:
            raise ReadError('Seat layout not found in JSON')

        rows = []
        seats = []
        available = []

        # Parse categories, rows, and seats
        categories = seat_data.get('categories', [])
        for cat in categories:
            cat_name = cat.get('name', '')
            for row in cat.get('rows', []):
                row_name = row.get('name', '')
                row_id = f"{cat_name} / {row_name}"
                
                rows.append({
                    "id": row_id,
                    "category": cat_name,
                    "row": row_name
                })

                for seat in row.get('seats', []):
                    seat_num = str(seat.get('number', ''))
                    is_available = seat.get('status') == 1 # 1 = available
                    
                    seat_obj = {
                        "row_id": row_id,
                        "seat": seat_num,
                        "available": is_available
                    }
                    seats.append(seat_obj)
                    
                    if is_available:
                        available.append({
                            "row_id": row_id,
                            "seat": seat_num
                        })

        return {
            "rows": rows,
            "seats": seats,
            "available": available,
            "url": url
        }

    except cffi_requests.RequestsError as e:
        raise ReadError(f'Connection error: {str(e)}', 0, 60)
    except json.JSONDecodeError:
        raise ReadError('Failed to parse JSON from BMS', 0, 60)
    except Exception as e:
        if isinstance(e, ReadError):
            raise e
        raise ReadError(f'Unexpected error in seat reader: {str(e)}', 0, 60)
