# -*- coding: utf-8 -*-
from caches.main_cache import cache_object
from modules.kodi_utils import make_session
# from modules.kodi_utils import logger

session = make_session('https://mdblist.com/')

base_url = 'https://mdblist.com/api/?apikey=%s&i=%s'

def _fetch(api_key, imdb_id):
	try:
		response = session.get(base_url % (api_key, imdb_id), timeout=15)
		if response.status_code != 200: return {}
		return response.json() or {}
	except: return {}

def get_ratings(imdb_id, api_key, expiration=168):
	if not imdb_id or not str(imdb_id).startswith('tt') or not api_key: return {}
	string = 'mdblist_ratings_%s' % imdb_id
	raw = cache_object(_fetch, string, [api_key, imdb_id], json=False, expiration=expiration)
	return _parse(raw)

def _parse(raw):
	out = {}
	if not raw: return out
	for r in raw.get('ratings', []) or []:
		src, val, popular = r.get('source'), r.get('value'), r.get('popular')
		if src == 'imdb' and val is not None:
			out['imdb'] = float(val)
			if popular is not None: out['imdb_popular'] = int(popular)
		elif src == 'metacritic' and val is not None:
			out['metascore'] = float(val)
		elif src == 'tomatoes' and val is not None:
			out['rt_critic'] = float(val)
		elif src == 'tomatoesaudience' and val is not None:
			out['rt_audience'] = float(val)
		elif src == 'tmdb' and val is not None:
			out['tmdb'] = float(val) / 10.0 if float(val) > 10 else float(val)
	return out
