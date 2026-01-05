#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import tempfile
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, parse_qs, urlparse
from collections import defaultdict, Counter
from pathlib import Path
import pytz
import requests
from threading import Lock

from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, Response, url_for, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Avatar configuration - use Flask's static folder
ALLOWED_EXTS = ("jpg", "png", "webp")

def get_avatar_static_dir():
    """Get the correct avatar static directory path"""
    return Path(app.static_folder) / "avatars"

def convert_to_baidu_url(url):
    """Convert original URL to Baidu CDN proxy URL"""
    if not url:
        return url
    base_url = "https://image.baidu.com/search/down?url="
    return base_url + quote(url)

@app.template_global()
def avatar_src(from_uid: int | str, size: int = 96) -> str:
    """Get avatar source URL with local file priority and SVG fallback"""
    uid = str(from_uid) if from_uid is not None else ""
    # 1) 本地文件优先 - 检查 static/avatars/{uid}.{ext}
    avatar_dir = get_avatar_static_dir()
    for ext in ALLOWED_EXTS:
        p = avatar_dir / f"{uid}.{ext}"
        if p.exists():
            return url_for("static", filename=f"avatars/{uid}.{ext}")
    # 2) 回退首字母 SVG - 不再使用任何远程URL
    return url_for("avatar_svg", uid=uid, size=size)

# Configuration
NDJSON_PATH = os.environ.get('WEIBO_NDJSON', 'chat_records/all.ndjson')
TIMEZONE = os.environ.get('WEIBO_TZ', 'Asia/Shanghai')
PAGE_SIZE = int(os.environ.get('WEIBO_PAGE_SIZE', '50'))
AVATARS_CACHE_PATH = os.path.join(os.path.dirname(NDJSON_PATH), 'avatars.json')

# Global data storage
messages_data = []
users_cache = {}
all_types = set()
dataset_info = {}
avatars_cache = {}
update_lock = Lock()
update_progress = {'status': 'idle', 'progress': 0, 'message': '', 'result': None}

def load_and_process_data():
    """Load NDJSON data and build indexes"""
    global messages_data, users_cache, all_types, dataset_info
    
    if not os.path.exists(NDJSON_PATH):
        print(f"Warning: Data file {NDJSON_PATH} not found")
        return
    
    print(f"Loading data from {NDJSON_PATH}...")
    
    messages_data = []
    users_cache = {}
    user_message_counts = Counter()
    all_types = set()
    
    tz = pytz.timezone(TIMEZONE)
    
    with open(NDJSON_PATH, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
                
            try:
                data = json.loads(line)
                
                # Extract timestamp
                timestamp = None
                for time_field in ['time', 'ts', 'timestamp', 'created_at']:
                    if time_field in data:
                        timestamp = data[time_field]
                        break
                
                if timestamp is None:
                    continue
                
                # Convert timestamp to datetime
                if isinstance(timestamp, str):
                    try:
                        # Try parsing ISO format first
                        dt_utc = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        if dt_utc.tzinfo is None:
                            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                    except:
                        try:
                            # Try parsing as timestamp
                            timestamp = float(timestamp)
                            dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                        except:
                            continue
                else:
                    # Numeric timestamp
                    try:
                        # Handle both seconds and milliseconds
                        if timestamp > 1e12:  # Milliseconds
                            timestamp = timestamp / 1000
                        dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    except:
                        continue
                
                # Convert to local timezone
                dt_local = dt_utc.astimezone(tz)
                
                # Extract user info
                from_uid = data.get('from_uid') or data.get('uid') or data.get('user_id')
                if not from_uid:
                    continue
                
                from_uid = str(from_uid)
                
                # Get user info
                from_user = data.get('from_user', {})
                sender_name = (from_user.get('screen_name') or 
                             data.get('sender_nick') or 
                             data.get('nick') or 
                             from_uid)
                
                # Get avatar URL
                avatar_url = (from_user.get('profile_image_url') or 
                            from_user.get('avatar_large') or 
                            data.get('avatar_large'))
                
                # Cache user info
                if from_uid not in users_cache:
                    users_cache[from_uid] = {
                        'uid': from_uid,
                        'name': sender_name,
                        'avatar_url': avatar_url
                    }
                
                user_message_counts[from_uid] += 1
                
                # Extract message content
                text = (data.get('content') or data.get('text') or '').strip()
                
                # Extract message type
                msg_type = data.get('type')
                if msg_type:
                    msg_type = f"type:{msg_type}"
                    all_types.add(msg_type)
                
                # Check for images
                is_image = False
                image_urls = []
                pic_infos = data.get('pic_infos', {})
                if pic_infos:
                    is_image = True
                    # Handle both dict and list formats
                    if isinstance(pic_infos, dict):
                        for pic_id, pic_info in pic_infos.items():
                            if isinstance(pic_info, dict):
                                # Use original_pic if available, fallback to thumbnail
                                img_url = pic_info.get('original_pic') or pic_info.get('thumbnail_pic')
                                if img_url:
                                    image_urls.append(convert_to_baidu_url(img_url))
                    elif isinstance(pic_infos, list):
                        for pic_info in pic_infos:
                            if isinstance(pic_info, dict):
                                # Use original_pic if available, fallback to thumbnail
                                img_url = pic_info.get('original_pic') or pic_info.get('thumbnail_pic')
                                if img_url:
                                    image_urls.append(convert_to_baidu_url(img_url))
                
                # If no text but has images, add placeholder text
                if is_image and not text.strip():
                    text = "[分享图片]"
                
                # Handle recalled messages
                if msg_type == 'type:344':
                    text = f"撤回了一条消息"
                
                # Build message object
                message = {
                    'id': data.get('id') or data.get('mid') or f"msg_{line_num}",
                    'from_uid': from_uid,
                    'sender_name': sender_name,
                    'avatar_url': avatar_url,
                    'text': text,
                    'text_lower': text.lower(),
                    'sender_name_lower': sender_name.lower(),
                    'time_utc': dt_utc,
                    'time_local': dt_local,
                    'time_str': dt_local.strftime('%Y-%m-%d %H:%M:%S'),
                    'type': msg_type,
                    'is_image': is_image,
                    'image_urls': image_urls,
                    'media_type': data.get('media_type'),
                    'raw_data': data
                }
                
                messages_data.append(message)
                
            except Exception as e:
                print(f"Error processing line {line_num}: {e}")
                continue
    
    # Sort messages by time
    messages_data.sort(key=lambda x: x['time_utc'])
    
    # Update user cache with message counts
    for uid, count in user_message_counts.items():
        if uid in users_cache:
            users_cache[uid]['count'] = count
    
    # Build dataset info
    if messages_data:
        earliest = messages_data[0]['time_local']
        latest = messages_data[-1]['time_local']
        dataset_info = {
            'path': NDJSON_PATH,
            'timezone': TIMEZONE,
            'time_range': f"{earliest.strftime('%Y-%m-%d')} 至 {latest.strftime('%Y-%m-%d')}",
            'total_messages': len(messages_data),
            'total_users': len(users_cache)
        }
    
    print(f"Loaded {len(messages_data)} messages from {len(users_cache)} users")
    
    # Load avatar cache
    load_avatars_cache()

def load_avatars_cache():
    """Load avatar cache from disk"""
    global avatars_cache
    
    try:
        if os.path.exists(AVATARS_CACHE_PATH):
            with open(AVATARS_CACHE_PATH, 'r', encoding='utf-8') as f:
                avatars_cache = json.load(f)
            print(f"Loaded avatar cache with {len(avatars_cache)} users")
        else:
            avatars_cache = {}
            print("No avatar cache found, starting with empty cache")
    except Exception as e:
        print(f"Error loading avatar cache: {e}")
        avatars_cache = {}

def save_avatars_cache(cache_data):
    """Save avatar cache to disk atomically"""
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(AVATARS_CACHE_PATH), exist_ok=True)
        
        # Write to temporary file first
        temp_path = AVATARS_CACHE_PATH + '.tmp'
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        
        # Atomic replace
        if os.name == 'nt':  # Windows
            if os.path.exists(AVATARS_CACHE_PATH):
                os.remove(AVATARS_CACHE_PATH)
        os.rename(temp_path, AVATARS_CACHE_PATH)
        
        print(f"Saved avatar cache with {len(cache_data)} users")
        return True
    except Exception as e:
        print(f"Error saving avatar cache: {e}")
        return False

def get_user_avatar_info(uid):
    """Get avatar info for a user with fallback chain"""
    uid = str(uid)
    
    # First try avatar cache
    if uid in avatars_cache:
        cache_entry = avatars_cache[uid]
        return {
            'name': cache_entry.get('screen_name', uid),
            'avatar_url': cache_entry.get('avatar_url'),
            'source': 'cache'
        }
    
    # Fallback to users_cache
    if uid in users_cache:
        user_info = users_cache[uid]
        return {
            'name': user_info.get('name', uid),
            'avatar_url': user_info.get('avatar_url'),
            'source': 'inline'
        }
    
    # Final fallback
    return {
        'name': uid,
        'avatar_url': None,
        'source': 'fallback'
    }

def generate_svg_avatar(name, size=48):
    """Generate SVG avatar with user's first character"""
    if not name:
        name = '?'
    
    first_char = name[0].upper()
    
    # Generate color based on name hash
    color_hash = hashlib.md5(name.encode()).hexdigest()
    hue = int(color_hash[:2], 16) * 360 // 256
    
    svg = f'''<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg">
    <circle cx="{size//2}" cy="{size//2}" r="{size//2}" fill="hsl({hue}, 60%, 50%)"/>
    <text x="{size//2}" y="{size//2 + size//8}" text-anchor="middle" fill="white" 
          font-family="Arial, sans-serif" font-size="{size//2}" font-weight="bold">{first_char}</text>
</svg>'''
    
    return svg

def validate_avatar_url(url, timeout=5):
    """Validate if avatar URL is accessible"""
    if not url:
        return False
    
    try:
        headers = {
            'Referer': 'https://api.weibo.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        return response.status_code == 200
    except:
        return False

def download_avatar_image(url, uid, timeout=10):
    """Download avatar image and save locally"""
    if not url:
        return None
    
    try:
        # Create avatars directory
        avatars_dir = os.path.join(os.path.dirname(NDJSON_PATH), 'avatars')
        os.makedirs(avatars_dir, exist_ok=True)
        
        # Generate filename from URL
        import urllib.parse
        parsed_url = urllib.parse.urlparse(url)
        file_ext = os.path.splitext(parsed_url.path)[1] or '.jpg'
        filename = f"{uid}{file_ext}"
        filepath = os.path.join(avatars_dir, filename)
        
        # Skip if file already exists and is recent (less than 7 days old)
        if os.path.exists(filepath):
            file_age = datetime.now().timestamp() - os.path.getmtime(filepath)
            if file_age < 7 * 24 * 3600:  # 7 days
                return f"/static/avatars/{filename}"
        
        # Download image with proper headers
        headers = {
            'Referer': 'https://api.weibo.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, timeout=timeout, headers=headers, stream=True)
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            return None
        
        # Save image
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"Downloaded avatar for {uid}: {filename}")
        return f"/static/avatars/{filename}"
        
    except Exception as e:
        print(f"Failed to download avatar for {uid}: {e}")
        return None

def update_avatars_from_messages(validate_urls=False, limit=None, dry_run=False):
    """Update avatar cache by scanning all messages"""
    global update_progress, avatars_cache
    
    with update_lock:
        update_progress['status'] = 'running'
        update_progress['progress'] = 0
        update_progress['message'] = '开始扫描消息...'
        
        try:
            # Scan messages to build candidate cache
            candidates = {}
            total_messages = len(messages_data)
            if limit:
                total_messages = min(total_messages, limit)
            
            for i, message in enumerate(messages_data[:total_messages]):
                if i % 100 == 0:
                    progress = int((i / total_messages) * 50)  # First 50% for scanning
                    update_progress['progress'] = progress
                    update_progress['message'] = f'扫描消息 {i}/{total_messages}'
                
                uid = message['from_uid']
                raw_data = message.get('raw_data', {})
                from_user = raw_data.get('from_user', {})
                
                # Extract latest info for this user
                screen_name = (from_user.get('screen_name') or 
                             raw_data.get('sender_nick') or 
                             raw_data.get('nick') or 
                             uid)
                
                avatar_url = (from_user.get('profile_image_url') or 
                            from_user.get('avatar_large') or 
                            raw_data.get('avatar_large'))
                
                # Update candidate if this is newer or better
                if uid not in candidates or message['time_utc'] > candidates[uid]['last_seen']:
                    candidates[uid] = {
                        'uid': uid,
                        'screen_name': screen_name,
                        'avatar_url': avatar_url,
                        'last_seen': message['time_utc'],
                        'message_id': message['id'],
                        'sources': [message['id']]
                    }
                else:
                    # Add to sources for audit
                    if len(candidates[uid]['sources']) < 3:
                        candidates[uid]['sources'].append(message['id'])
            
            # Download and validate avatars if requested
            if validate_urls:
                valid_candidates = {}
                total_candidates = len(candidates)
                downloaded_count = 0
                
                for i, (uid, candidate) in enumerate(candidates.items()):
                    progress = 50 + int((i / total_candidates) * 40)  # 50-90% for validation and download
                    update_progress['progress'] = progress
                    update_progress['message'] = f'处理头像 {i+1}/{total_candidates} (已下载 {downloaded_count})'
                    
                    if candidate['avatar_url']:
                        # Try to download the avatar
                        local_path = download_avatar_image(candidate['avatar_url'], uid)
                        if local_path:
                            # Successfully downloaded, use local path
                            candidate['avatar_url'] = local_path
                            candidate['local_avatar'] = True
                            valid_candidates[uid] = candidate
                            downloaded_count += 1
                        elif validate_avatar_url(candidate['avatar_url']):
                            # URL is valid but download failed, keep original URL
                            valid_candidates[uid] = candidate
                        elif uid in avatars_cache:
                            # Keep old avatar if new one is invalid
                            old_entry = avatars_cache[uid].copy()
                            old_entry['sources'] = candidate['sources']
                            valid_candidates[uid] = old_entry
                        else:
                            # No avatar
                            candidate['avatar_url'] = None
                            valid_candidates[uid] = candidate
                    elif uid in avatars_cache:
                        # Keep existing cache entry
                        old_entry = avatars_cache[uid].copy()
                        old_entry['sources'] = candidate['sources']
                        valid_candidates[uid] = old_entry
                    else:
                        # No avatar
                        candidate['avatar_url'] = None
                        valid_candidates[uid] = candidate
                
                candidates = valid_candidates
                update_progress['message'] = f'头像处理完成，共下载 {downloaded_count} 个头像'
            
            # Merge with existing cache
            update_progress['progress'] = 85
            update_progress['message'] = '合并缓存数据...'
            
            new_cache = avatars_cache.copy()
            changes = {'added': 0, 'updated': 0, 'downloaded': 0, 'details': []}
            
            for uid, candidate in candidates.items():
                if uid not in new_cache:
                    # New user
                    new_cache[uid] = {
                        'uid': uid,
                        'screen_name': candidate['screen_name'],
                        'avatar_url': candidate['avatar_url'],
                        'updated_at': datetime.now().isoformat(),
                        'sources': candidate['sources']
                    }
                    changes['added'] += 1
                    
                    # Add detailed feedback about avatar status
                    if candidate.get('local_avatar'):
                        changes['downloaded'] += 1
                        changes['details'].append(f"新增用户并下载头像: {candidate['screen_name']} ({uid})")
                    elif candidate['avatar_url']:
                        changes['details'].append(f"新增用户(使用原始头像URL): {candidate['screen_name']} ({uid})")
                    else:
                        changes['details'].append(f"新增用户(无头像): {candidate['screen_name']} ({uid})")
                else:
                    # Existing user - check for updates
                    old_entry = new_cache[uid]
                    updated = False
                    
                    if old_entry.get('screen_name') != candidate['screen_name']:
                        old_entry['previous_names'] = old_entry.get('previous_names', [])
                        if old_entry.get('screen_name'):
                            old_entry['previous_names'].append(old_entry['screen_name'])
                        old_entry['screen_name'] = candidate['screen_name']
                        updated = True
                    
                    if candidate['avatar_url'] and old_entry.get('avatar_url') != candidate['avatar_url']:
                        old_entry['avatar_url'] = candidate['avatar_url']
                        updated = True
                    
                    if updated:
                        old_entry['updated_at'] = datetime.now().isoformat()
                        old_entry['sources'] = candidate['sources']
                        changes['updated'] += 1
                        
                        # Add detailed feedback about update status
                        if candidate.get('local_avatar'):
                            changes['downloaded'] += 1
                            changes['details'].append(f"更新用户并下载头像: {candidate['screen_name']} ({uid})")
                        elif candidate['avatar_url']:
                            changes['details'].append(f"更新用户信息: {candidate['screen_name']} ({uid})")
                        else:
                            changes['details'].append(f"更新用户信息(移除头像): {candidate['screen_name']} ({uid})")
            
            # Save to disk
            if not dry_run:
                update_progress['progress'] = 95
                update_progress['message'] = '保存缓存文件...'
                
                if save_avatars_cache(new_cache):
                    avatars_cache = new_cache
                else:
                    raise Exception("Failed to save cache file")
            
            # Complete
            update_progress['status'] = 'completed'
            update_progress['progress'] = 100
            update_progress['message'] = '更新完成'
            update_progress['result'] = changes
            
            return changes
            
        except Exception as e:
            update_progress['status'] = 'error'
            update_progress['message'] = f'更新失败: {str(e)}'
            raise

def highlight_text(text, keyword):
    """Highlight keyword in text with HTML mark tags"""
    if not keyword or not text:
        return text
    
    # First, remove any existing highlight tags to avoid nested highlighting
    text = re.sub(r'<mark[^>]*>', '', text)
    text = re.sub(r'</mark>', '', text)
    
    # Escape HTML in text first
    import html
    text = html.escape(text)
    
    # Case-insensitive replacement with mark tags
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    highlighted = pattern.sub(lambda m: f'<mark class="highlight">{m.group()}</mark>', text)
    
    return highlighted

def format_dash_separators(text):
    """Format dash separators as separate lines"""
    if not text:
        return text
    
    # Pattern to match dash separators: multiple dashes with optional spaces
    # Matches patterns like: - - - - - - - - - - - - - - -
    dash_pattern = r'(\s*-\s*){3,}'
    
    # Replace dash separators with line breaks before and after
    formatted_text = re.sub(dash_pattern, lambda m: f'<br>{m.group().strip()}<br>', text)
    
    return formatted_text

# Register custom Jinja2 filters
@app.template_filter('format_separators')
def format_separators_filter(text):
    """Jinja2 filter to format dash separators"""
    return format_dash_separators(text)

def apply_filters(filters):
    """Apply filters to messages and return filtered results"""
    filtered_messages = messages_data[:]
    
    # Date filters
    if filters.get('start'):
        try:
            start_date = datetime.strptime(filters['start'], '%Y-%m-%d')
            start_date = pytz.timezone(TIMEZONE).localize(start_date)
            start_utc = start_date.astimezone(timezone.utc)
            filtered_messages = [m for m in filtered_messages if m['time_utc'] >= start_utc]
        except ValueError:
            pass
    
    if filters.get('end'):
        try:
            end_date = datetime.strptime(filters['end'], '%Y-%m-%d')
            # End date should include the whole day
            end_date = end_date.replace(hour=23, minute=59, second=59)
            end_date = pytz.timezone(TIMEZONE).localize(end_date)
            end_utc = end_date.astimezone(timezone.utc)
            filtered_messages = [m for m in filtered_messages if m['time_utc'] <= end_utc]
        except ValueError:
            pass
    
    # Speaker filters
    if filters.get('uids'):
        uid_list = [uid.strip() for uid in filters['uids'].split(',') if uid.strip()]
        if uid_list:
            filtered_messages = [m for m in filtered_messages if m['from_uid'] in uid_list]
    
    # Type filters
    if filters.get('types'):
        type_list = filters['types'] if isinstance(filters['types'], list) else [filters['types']]
        if type_list:
            filtered_messages = [m for m in filtered_messages if m['type'] in type_list]
    
    # Keyword filter
    if filters.get('q'):
        keyword = filters['q'].lower()
        filtered_messages = [
            m for m in filtered_messages 
            if keyword in m['text_lower'] or keyword in m['sender_name_lower']
        ]
        
        # Apply highlighting
        for message in filtered_messages:
            message['text'] = highlight_text(message['text'], filters['q'])
            message['sender_name'] = highlight_text(message['sender_name'], filters['q'])
    
    return filtered_messages

def build_pagination(total_items, page, per_page, base_url, query_params):
    """Build pagination info"""
    total_pages = (total_items + per_page - 1) // per_page
    
    # Calculate page range for display
    page_range = []
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)
    
    if start_page > 1:
        page_range.append(1)
        if start_page > 2:
            page_range.append('...')
    
    for p in range(start_page, end_page + 1):
        page_range.append(p)
    
    if end_page < total_pages:
        if end_page < total_pages - 1:
            page_range.append('...')
        page_range.append(total_pages)
    
    # Build URLs
    def build_url(page_num):
        params = query_params.copy()
        params['page'] = page_num
        return f"{base_url}?{urlencode(params)}"
    
    page_urls = {}
    for p in page_range:
        if isinstance(p, int):
            page_urls[p] = build_url(p)
    
    return {
        'page': page,
        'pages': total_pages,
        'total': total_items,
        'per_page': per_page,
        'start_item': (page - 1) * per_page + 1,
        'end_item': min(page * per_page, total_items),
        'page_range': page_range,
        'page_urls': page_urls,
        'first_url': build_url(1) if page > 1 else None,
        'prev_url': build_url(page - 1) if page > 1 else None,
        'next_url': build_url(page + 1) if page < total_pages else None,
        'last_url': build_url(total_pages) if page < total_pages else None,
    }

@app.route('/')
def index():
    """Main page with message list"""
    # Parse filters from query parameters
    filters = {
        'start': request.args.get('start', ''),
        'end': request.args.get('end', ''),
        'q': request.args.get('q', ''),
        'uids': request.args.get('uids', ''),
        'types': request.args.getlist('types'),
        'per_page': int(request.args.get('per_page', PAGE_SIZE))
    }
    
    # Set default start date to today if no filters are applied
    if not any([filters['start'], filters['q'], filters['uids'], filters['types']]):
        today = datetime.now().strftime('%Y-%m-%d')
        filters['start'] = today
    
    page = int(request.args.get('page', 1))
    
    # Apply filters
    filtered_messages = apply_filters(filters)
    
    # Enhance messages with avatar cache info
    for message in filtered_messages:
        avatar_info = get_user_avatar_info(message['from_uid'])
        message['sender_name'] = avatar_info['name']
        message['avatar_url'] = avatar_info['avatar_url']
        message['avatar_source'] = avatar_info['source']
        
        # Set fallback avatar URL if none available
        if not message['avatar_url']:
            message['avatar_fallback'] = url_for('avatar_svg', uid=message['from_uid'])
    
    # Pagination
    total_results = len(filtered_messages)
    start_idx = (page - 1) * filters['per_page']
    end_idx = start_idx + filters['per_page']
    page_messages = filtered_messages[start_idx:end_idx]
    
    # Build pagination info
    query_params = {k: v for k, v in request.args.items() if k != 'page'}
    pagination = build_pagination(total_results, page, filters['per_page'], '/', query_params)
    
    # Get available speakers (sorted by message count)
    available_speakers = sorted(
        users_cache.values(), 
        key=lambda x: x.get('count', 0), 
        reverse=True
    )
    
    # Get selected speakers info
    selected_speakers = []
    current_speaker_uids = []
    if filters['uids']:
        current_speaker_uids = [uid.strip() for uid in filters['uids'].split(',') if uid.strip()]
        selected_speakers = [
            {'uid': uid, 'name': users_cache.get(uid, {}).get('name', uid)}
            for uid in current_speaker_uids
            if uid in users_cache
        ]
    
    # Build export URL
    export_params = {k: v for k, v in request.args.items()}
    export_url = url_for('export_markdown', **export_params)
    
    return render_template('index.html',
        messages=page_messages,
        filters=filters,
        pagination=pagination,
        total_results=total_results,
        available_speakers=available_speakers,
        selected_speakers=selected_speakers,
        selected_speakers_count=len(selected_speakers),
        current_speaker_uids=current_speaker_uids,
        all_types=sorted(all_types),
        export_url=export_url,
        dataset_info=dataset_info
    )

@app.route('/users.json')
def users_json():
    """Return users data for speaker selection"""
    speakers = sorted(
        users_cache.values(), 
        key=lambda x: x.get('count', 0), 
        reverse=True
    )
    return jsonify(speakers)

@app.route('/export.md')
def export_markdown():
    """Export filtered messages as Markdown"""
    # Parse same filters as main page
    filters = {
        'start': request.args.get('start', ''),
        'end': request.args.get('end', ''),
        'q': request.args.get('q', ''),
        'uids': request.args.get('uids', ''),
        'types': request.args.getlist('types')
    }
    
    # Apply filters (without highlighting for export)
    export_filters = filters.copy()
    export_filters.pop('q', None)  # Remove keyword to avoid highlighting
    filtered_messages = apply_filters(export_filters)
    
    # Apply keyword filter manually without highlighting
    if filters.get('q'):
        keyword = filters['q'].lower()
        filtered_messages = [
            m for m in filtered_messages 
            if keyword in m['text_lower'] or keyword in m['sender_name_lower']
        ]
    
    # Build Markdown content
    lines = []
    
    # Header
    lines.append("# 微博群聊记录导出")
    lines.append("")
    lines.append(f"**数据源**: {dataset_info.get('path', 'N/A')}")
    lines.append(f"**时区**: {dataset_info.get('timezone', 'N/A')}")
    lines.append(f"**导出时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    
    # Filter summary
    filter_summary = []
    if filters['start']:
        filter_summary.append(f"起始日期: {filters['start']}")
    if filters['end']:
        filter_summary.append(f"结束日期: {filters['end']}")
    if filters['uids']:
        uid_list = [uid.strip() for uid in filters['uids'].split(',') if uid.strip()]
        speaker_names = [users_cache.get(uid, {}).get('name', uid) for uid in uid_list]
        filter_summary.append(f"发言人: {', '.join(speaker_names)}")
    if filters['types']:
        filter_summary.append(f"消息类型: {', '.join(filters['types'])}")
    if filters['q']:
        filter_summary.append(f"关键词: {filters['q']}")
    
    if filter_summary:
        lines.append("**筛选条件**:")
        for item in filter_summary:
            lines.append(f"- {item}")
        lines.append("")
    
    lines.append(f"**消息总数**: {len(filtered_messages)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # Messages
    for message in filtered_messages:
        # Format: - **[YYYY-MM-DD HH:MM:SS TZ] screen_name**: 文本
        time_str = message['time_str']
        sender = message['sender_name']
        text = message['text']
        
        # Clean text for markdown (remove HTML tags from highlighting)
        text = re.sub(r'<mark[^>]*>', '', text)
        text = re.sub(r'</mark>', '', text)
        
        # Escape markdown special characters
        text = text.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
        
        # Handle special message types
        if message['type'] == 'type:344':
            text = f"*{text}*"  # Italic for recalled messages
        elif message['is_image']:
            if message['image_urls']:
                image_links = ' '.join([f"[图片]({url})" for url in message['image_urls']])
                text = f"{text}  \n{image_links}"
        
        # Convert newlines to markdown line breaks
        text = text.replace('\n', '  \n')
        
        line = f"- **[{time_str}] {sender}**: {text}"
        lines.append(line)
    
    markdown_content = '\n'.join(lines)
    
    # Return as downloadable file
    response = Response(
        markdown_content,
        mimetype='text/markdown',
        headers={
            'Content-Disposition': 'attachment; filename=chat_export.md'
        }
    )
    return response


@app.route('/export-daily.md')
def export_daily_markdown():
    """Export daily messages as Markdown (3AM to 3AM next day)"""
    date_str = request.args.get('date')
    if not date_str:
        return "Missing date parameter", 400
    
    try:
        # Parse the selected date
        selected_date = datetime.strptime(date_str, '%Y-%m-%d')
        tz = pytz.timezone(TIMEZONE)
        
        # Set start time to 3AM of the selected date
        start_time = tz.localize(selected_date.replace(hour=3, minute=0, second=0, microsecond=0))
        start_utc = start_time.astimezone(timezone.utc)
        
        # Set end time to 3AM of the next day
        end_time = start_time + timedelta(days=1)
        end_utc = end_time.astimezone(timezone.utc)
        
        # Filter messages within the time range
        filtered_messages = [
            m for m in messages_data 
            if start_utc <= m['time_utc'] < end_utc
        ]
        
        # Enhance messages with avatar cache info
        for message in filtered_messages:
            avatar_info = get_user_avatar_info(message['from_uid'])
            message['sender_name'] = avatar_info['name']
            message['avatar_url'] = avatar_info['avatar_url']
        
        # Build Markdown content
        lines = []
        
        # Header
        lines.append("# 微博群聊一日记录")
        lines.append("")
        lines.append(f"**数据源**: {dataset_info.get('path', 'N/A')}")
        lines.append(f"**时区**: {dataset_info.get('timezone', 'N/A')}")
        lines.append(f"**导出时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append(f"**记录日期**: {date_str}")
        lines.append(f"**时间范围**: {start_time.strftime('%Y-%m-%d %H:%M:%S')} 至 {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**消息总数**: {len(filtered_messages)}")
        lines.append("")
        lines.append("---")
        lines.append("")
        
        # Messages
        for message in filtered_messages:
            # Format: - **[HH:MM:SS] screen_name**: 文本 (只保留时间，去掉日期)
            time_str = message['time_local'].strftime('%H:%M:%S')
            sender = message['sender_name']
            text = message['text']
            
            # Escape markdown special characters
            text = text.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
            
            # Handle special message types
            if message['type'] == 'type:344':
                text = f"*{text}*"  # Italic for recalled messages
            elif message['is_image']:
                if message['image_urls']:
                    image_links = ' '.join([f"[图片]({url})" for url in message['image_urls']])
                    text = f"{text}  \n{image_links}"
            
            # Convert newlines to markdown line breaks
            text = text.replace('\n', '  \n')
            
            line = f"- **[{time_str}] {sender}**: {text}"
            lines.append(line)
        
        markdown_content = '\n'.join(lines)
        
        # Generate filename with date
        filename = f"chat_daily_{date_str}.md"
        
        # Return as downloadable file
        response = Response(
            markdown_content,
            mimetype='text/markdown',
            headers={
                'Content-Disposition': f'attachment; filename={filename}'
            }
        )
        return response
        
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD", 400
    except Exception as e:
        return f"Error processing request: {str(e)}", 500


@app.route('/context/<message_id>')
def view_context(message_id):
    """View context for a specific message (limited initial window)"""
    # Find the target message and its index
    target_idx = -1
    for i, m in enumerate(messages_data):
        if str(m['id']) == str(message_id):
            target_idx = i
            break
            
    if target_idx == -1:
        return "Message not found", 404
    
    target_msg = messages_data[target_idx]
    
    # Get the day range (3AM to 3AM)
    msg_time = target_msg['time_local']
    if msg_time.hour < 3:
        base_date = (msg_time - timedelta(days=1)).date()
    else:
        base_date = msg_time.date()
    
    tz = pytz.timezone(TIMEZONE)
    start_time = tz.localize(datetime.combine(base_date, datetime.min.time()).replace(hour=3))
    start_utc = start_time.astimezone(timezone.utc)
    end_time = start_time + timedelta(days=1)
    end_utc = end_time.astimezone(timezone.utc)
    
    # Find all messages for that day to know the boundaries
    day_messages_indices = [
        i for i, m in enumerate(messages_data)
        if start_utc <= m['time_utc'] < end_utc
    ]
    
    if not day_messages_indices:
        return "No messages found for this day", 404
        
    day_start_idx = day_messages_indices[0]
    day_end_idx = day_messages_indices[-1]
    
    # Initial window: target -10/+30 messages, but stay within day boundaries
    view_start_idx = max(day_start_idx, target_idx - 10)
    view_end_idx = min(day_end_idx, target_idx + 30)
    
    context_messages = messages_data[view_start_idx:view_end_idx + 1]
    
    # Enhance messages with avatar info
    for message in context_messages:
        avatar_info = get_user_avatar_info(message['from_uid'])
        message['sender_name'] = avatar_info['name']
        message['avatar_url'] = avatar_info['avatar_url']
        message['avatar_source'] = avatar_info['source']
        if not message['avatar_url']:
            message['avatar_fallback'] = url_for('avatar_svg', uid=message['from_uid'])

    return render_template('context.html',
        messages=context_messages,
        target_id=message_id,
        date_str=base_date.strftime('%Y-%m-%d'),
        dataset_info=dataset_info,
        has_more_above=(view_start_idx > day_start_idx),
        has_more_below=(view_end_idx < day_end_idx),
        current_start_idx=view_start_idx,
        current_end_idx=view_end_idx,
        day_start_idx=day_start_idx,
        day_end_idx=day_end_idx
    )

@app.route('/context/more')
def load_more_context():
    """API to load more messages for context"""
    start_idx = int(request.args.get('start_idx'))
    end_idx = int(request.args.get('end_idx'))
    direction = request.args.get('direction') # 'above' or 'below'
    day_start = int(request.args.get('day_start'))
    day_end = int(request.args.get('day_end'))
    
    window_size = 30
    
    if direction == 'above':
        new_start = max(day_start, start_idx - window_size)
        new_end = start_idx - 1
        messages = messages_data[new_start:new_end + 1]
        has_more = (new_start > day_start)
        updated_idx = new_start
    else:
        new_start = end_idx + 1
        new_end = min(day_end, end_idx + window_size)
        messages = messages_data[new_start:new_end + 1]
        has_more = (new_end < day_end)
        updated_idx = new_end

    # Enhance messages
    for message in messages:
        avatar_info = get_user_avatar_info(message['from_uid'])
        message['sender_name'] = avatar_info['name']
        message['avatar_url'] = avatar_info['avatar_url']
        message['avatar_source'] = avatar_info['source']
        if not message['avatar_url']:
            message['avatar_fallback'] = url_for('avatar_svg', uid=message['from_uid'])
            
    # Render only the message cards
    html = "".join([render_template('_message_card.html', message=m) for m in messages])
    
    return jsonify({
        'html': html,
        'has_more': has_more,
        'updated_idx': updated_idx
    })

@app.route('/avatar/<uid>.svg')
def avatar_svg(uid):
    """Generate SVG avatar for user"""
    user_info = get_user_avatar_info(uid)
    size = int(request.args.get('size', 48))
    
    svg_content = generate_svg_avatar(user_info['name'], size)
    
    return Response(
        svg_content,
        mimetype='image/svg+xml',
        headers={
            'Cache-Control': 'public, max-age=3600'
        }
    )


@app.get("/__where")
def where():
    """Diagnostic endpoint to check static file paths"""
    root = Path(app.root_path)
    static_root = Path(app.static_folder)
    avatar_dir = get_avatar_static_dir()
    target = static_root / "avatars" / "1823418263.jpg"
    
    # Check what files actually exist in the avatars directory
    existing_files = []
    if avatar_dir.exists():
        existing_files = [f.name for f in avatar_dir.iterdir() if f.is_file()][:10]  # First 10 files
    
    return jsonify({
        "app_root": str(root),
        "static_folder": str(static_root),
        "target_exists": target.exists(),
        "target_path": str(target),
        "static_url_path": app.static_url_path,
        "avatar_static_dir": str(avatar_dir),
        "avatar_static_dir_exists": avatar_dir.exists(),
        "existing_files_sample": existing_files,
    })

if __name__ == '__main__':
    load_and_process_data()
    app.run(debug=True, host='0.0.0.0', port=5000,
            extra_files=['chat_records/all.ndjson'])
