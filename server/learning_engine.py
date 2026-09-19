import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict

class LearningEngine:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.browser_data = self.data_dir / 'browser_data.json'
        self.data = self._load_data()
        
    def _load_data(self):
        if self.browser_data.exists():
            with open(self.browser_data, 'r') as f:
                return json.load(f)
        return {
            'visits': [],
            'domains': {},
            'patterns': {},
            'preferences': {},
            'interactions': [],
            'topics': Counter()
        }
    
    def _save_data(self):
        with open(self.browser_data, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def get_stats(self):
        return {
            'total_visits': len(self.data.get('visits', [])),
            'unique_domains': len(self.data.get('domains', {})),
            'patterns_learned': len(self.data.get('patterns', {})),
            'topics_tracked': len(self.data.get('topics', {}))
        }
    
    def get_learning_summary(self):
        return {
            'stats': self.get_stats(),
            'top_domains': self._get_top_domains(10),
            'preferences': self.data.get('preferences', {}),
            'recent_activity': self.data.get('visits', [])[-10:]
        }
    
    def get_patterns(self):
        return self.data.get('patterns', {})
    
    def weekly_report(self):
        """A 'what I learned about you this week' recap computed from the last
        7 days of browsing data: domains, categories, time-of-day patterns,
        weekday usage and chats."""
        now = datetime.now()
        week_ago = now - timedelta(days=7)

        def _ts(iso):
            try:
                return datetime.fromisoformat(str(iso).replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                return None

        visits = self.data.get('visits', [])
        week = [v for v in visits if (_ts(v.get('timestamp')) or now) >= week_ago]

        top_domains = Counter()
        categories = Counter()
        hours = Counter()
        weekdays = Counter()
        for v in week:
            if v.get('domain'):
                top_domains[v['domain']] += 1
            if v.get('category'):
                categories[v['category']] += 1
            ts = _ts(v.get('timestamp'))
            if ts:
                hours[ts.strftime('%H:00')] += 1
                weekdays[ts.strftime('%A')] += 1

        chats = 0
        for it in self.data.get('interactions', []):
            if it.get('user_message') and (_ts(it.get('timestamp')) or now) >= week_ago:
                chats += 1

        def _bars(counter, limit):
            return [{'label': k, 'count': c} for k, c in counter.most_common(limit)]

        peak_hour = hours.most_common(1)
        peak_day = weekdays.most_common(1)

        parts = []
        if len(week) == 1:
            parts.append('1 visit')
        else:
            parts.append(f"{len(week)} visits")
        parts.append(f"across {len(top_domains)} domain{'s' if len(top_domains) != 1 else ''}")
        top_cats = [c for c, _ in categories.most_common(3)]
        if top_cats:
            parts.append('interests leaning toward ' + ', '.join(top_cats))
        if peak_day:
            parts.append(f"busiest on {peak_day[0][0]}")
        if peak_hour:
            parts.append(f"peak browsing around {peak_hour[0][0]}")
        narrative = 'This week I learned from ' + ', '.join(parts) + ('.' if parts else '.')

        return {
            'since': week_ago.isoformat(),
            'week_visits': len(week),
            'unique_domains': len(top_domains),
            'top_domains': _bars(top_domains, 8),
            'categories': _bars(categories, 6),
            'hourly': [{'hour': h, 'count': c} for h, c in sorted(hours.items())],
            'weekdays': _bars(weekdays, 7),
            'most_active_hour': peak_hour[0][0] if peak_hour else None,
            'most_active_day': peak_day[0][0] if peak_day else None,
            'chats_this_week': chats,
            'narrative': narrative,
        }

    def process_browser_data(self, event_data):
        event_type = event_data.get('type', 'unknown')
        
        if event_type == 'navigation':
            self._record_visit(event_data)
        elif event_type == 'activity':
            self._record_activity(event_data)
        elif event_type == 'tabs':
            self._record_tabs(event_data)
        
    def _record_visit(self, data):
        url = data.get('url', '')
        title = data.get('title', '')
        timestamp = data.get('timestamp', datetime.now().isoformat())

        domain = self._extract_domain(url)
        categories = self._categorize_domain(domain, title) if domain else []
        primary_cat = categories[0] if categories else 'general'

        visit = {
            'url': url,
            'title': title,
            'timestamp': timestamp,
            'duration': data.get('duration', 0),
            'domain': domain,
            'category': primary_cat,
        }

        if domain and domain in self.data['domains']:
            mins = self._minutes_since(self.data['domains'][domain].get('last_visited'), timestamp)
            visit['visit_count'] = self.data['domains'][domain]['visits'] + 1
            visit['mins_since_last'] = max(1, mins)
        else:
            visit['visit_count'] = 1
            visit['mins_since_last'] = 24 * 60
        visit['engagement'] = 1.0 if data.get('duration', 0) >= 15 else 0.3

        self.data['visits'].append(visit)

        if domain:
            if domain not in self.data['domains']:
                self.data['domains'][domain] = {
                    'visits': 0,
                    'total_duration': 0,
                    'last_visited': timestamp,
                    'titles': []
                }
            self.data['domains'][domain]['visits'] += 1
            self.data['domains'][domain]['total_duration'] += data.get('duration', 0)
            self.data['domains'][domain]['last_visited'] = timestamp
            if title and title not in self.data['domains'][domain]['titles'][:10]:
                self.data['domains'][domain]['titles'].append(title)

            self._learn_pattern(domain, timestamp)
            self._update_preferences(domain, title)

        if len(self.data['visits']) > 5000:
            self.data['visits'] = self.data['visits'][-5000:]

        self._save_data()

    def _minutes_since(self, prev_ts, cur_ts):
        if not prev_ts:
            return 24 * 60
        try:
            from datetime import datetime as dt
            p = dt.fromisoformat(str(prev_ts).replace('Z', '+00:00'))
            c = dt.fromisoformat(str(cur_ts).replace('Z', '+00:00'))
            return max(1, int((c - p).total_seconds() // 60))
        except Exception:
            return 24 * 60
    
    def _extract_domain(self, url):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.netloc if parsed.netloc else None
        except:
            return None
    
    def _record_activity(self, data):
        activity = {
            'type': data.get('activity_type', 'unknown'),
            'value': data.get('value', ''),
            'timestamp': data.get('timestamp', datetime.now().isoformat())
        }
        self.data['interactions'].append(activity)
        if len(self.data['interactions']) > 1000:
            self.data['interactions'] = self.data['interactions'][-1000:]
        self._save_data()
    
    def _record_tabs(self, data):
        tabs = data.get('tabs', [])
        for tab in tabs:
            domain = self._extract_domain(tab.get('url', ''))
            if domain and domain in self.data['domains']:
                self.data['domains'][domain]['visits'] += 1
        self._save_data()
    
    def _learn_pattern(self, domain, timestamp):
        hour = timestamp[11:13] if len(timestamp) >= 13 else '00'
        day = timestamp[:10]
        
        if 'hourly' not in self.data['patterns']:
            self.data['patterns']['hourly'] = Counter()
        if 'daily' not in self.data['patterns']:
            self.data['patterns']['daily'] = Counter()
        
        self.data['patterns']['hourly'][f'{hour}:00'] += 1
        self.data['patterns']['daily'][day] += 1
    
    def _update_preferences(self, domain, title):
        if 'preferences' not in self.data:
            self.data['preferences'] = {}
        
        categories = self._categorize_domain(domain, title)
        for category in categories:
            if category not in self.data['preferences']:
                self.data['preferences'][category] = {'count': 0, 'domains': []}
            self.data['preferences'][category]['count'] += 1
            if domain not in self.data['preferences'][category]['domains']:
                self.data['preferences'][category]['domains'].append(domain)
    
    def _categorize_domain(self, domain, title):
        domain_lower = domain.lower()
        title_lower = (title or '').lower()
        
        categories = []
        category_rules = {
            'news': ['news', 'cnn', 'bbc', 'reuters', 'nyt'],
            'social': ['facebook', 'twitter', 'instagram', 'reddit', 'linkedin'],
            'entertainment': ['youtube', 'netflix', 'spotify', 'twitch', 'hulu'],
            'shopping': ['amazon', 'ebay', 'walmart', 'etsy', 'shopify'],
            'education': ['edu', 'wikipedia', 'khanacademy', 'coursera', 'udemy'],
            'tech': ['github', 'stackoverflow', 'medium', 'dev', 'techcrunch'],
            'finance': ['bank', 'paypal', 'stripe', 'investing', 'stock']
        }
        
        for category, keywords in category_rules.items():
            if any(keyword in domain_lower or keyword in title_lower for keyword in keywords):
                categories.append(category)
        
        return categories if categories else ['general']
    
    def _get_top_domains(self, limit=10):
        sorted_domains = sorted(
            self.data.get('domains', {}).items(),
            key=lambda x: x[1]['visits'],
            reverse=True
        )
        return [
            {
                'domain': domain,
                'visits': details['visits'],
                'last_visited': details['last_visited']
            }
            for domain, details in sorted_domains[:limit]
        ]
    
    def log_interaction(self, interaction):
        if 'interactions' not in self.data:
            self.data['interactions'] = []
        self.data['interactions'].append(interaction)
        if len(self.data['interactions']) > 1000:
            self.data['interactions'] = self.data['interactions'][-1000:]
        self._save_data()
