import json
import re
import html
import base64
import threading
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import random

import requests

class AIAgent:
    # Pace every Wikipedia API call so bursts never trip Wikimedia's 429
    # throttle (the 24/7 trainer is already using its own request budget).
    _WIKI_LOCK = threading.Lock()
    _LAST_WIKI = 0.0
    _WIKI_PACE = 0.7  # seconds minimum between outbound wiki calls

    # Short/ambiguous aliases -> the right encyclopedia article (avoids hitting
    # disambiguation or shopping pages that Wikipedia's search ranks too high).
    SYNONYM_ARTICLE = {
        'pc': 'Personal computer',
        'cpu': 'Central processing unit',
        'gpu': 'Graphics processing unit',
        'ram': 'Random-access memory',
        'mobo': 'Motherboard',
        'psu': 'Power supply unit (computer)',
        'os': 'Operating system',
    }
    # Parts/composition questions -> the article that enumerates components.
    PARTS_ARTICLE = {
        'pc': 'Computer hardware', 'pcs': 'Computer hardware',
        'computer': 'Computer hardware', 'computers': 'Computer hardware',
        'laptop': 'Computer hardware', 'laptops': 'Computer hardware',
        'desktop': 'Computer hardware', 'desktops': 'Computer hardware',
        'notebook': 'Computer hardware', 'notebooks': 'Computer hardware',
        'workstation': 'Computer hardware', 'server': 'Computer hardware',
        'servers': 'Computer hardware', 'tower': 'Computer hardware',
        'car': 'Car', 'cars': 'Car',
        'automobile': 'Automobile', 'automobiles': 'Automobile',
        'vehicle': 'Vehicle', 'vehicles': 'Vehicle',
        'motorcycle': 'Motorcycle', 'motorcycles': 'Motorcycle',
        'bicycle': 'Bicycle', 'bike': 'Bicycle', 'bikes': 'Bicycle',
        'smartphone': 'Smartphone', 'smartphones': 'Smartphone',
        'phone': 'Smartphone',
    }
    # Never fetch research from these domains (adult, gambling, etc.).
    BLOCKED_DOMAIN_FRAGS = (
        'xvideo', 'xnxx', 'pornhub', 'porn', 'brazzers', 'redtube', 'youporn',
        'hentai', 'sexcam', '4tube', 'onlyfans', 'nudevista', 'spankbang',
        'tube8', 'adult', 'scheme', 'escort', 'gambling', 'bet365', 'betfair',
        'betway', 'paddypower', 'skybet', 'bovada', 'casino', 'pokerstars',
        'omegle', 'chatroulette', 'cam4', 'chaturbate', 'slut', 'hookup',
    )
    # Never include scraped sentences built around these words.
    EXPLICIT_WORDS = (
        'porn', 'porno', 'xvideo', 'xnxx', 'nude', 'naked', 'hentai',
        'penis', 'vagina', 'intercourse', 'fuck', 'fucking', 'blowjob',
        'handjob', 'masturbat', 'orgasm', 'sexcam', 'chaturbate', 'escort',
        'milf', 'anal sex', 'oral sex', 'assinante', 'nudevideos',
        'bimbofication', 'sexting', 'cock', 'dildo', 'vibrator',
    )

    def __init__(self, data_dir, learning_engine=None, training_engine=None, media_generator=None):
        self.data_dir = Path(data_dir)
        self.learning_engine = learning_engine
        self.training_engine = training_engine
        self.media_generator = media_generator
        self.config_file = self.data_dir / 'agent_config.json'
        self.history_file = self.data_dir / 'chat_history.json'
        self.context = {}
        self.config = self._load_config()
        self.history = self._load_history()
        self.memory = []  # conversational memory for multi-turn awareness

    def _load_config(self):
        if self.config_file.exists():
            with open(self.config_file, 'r') as f:
                return json.load(f)
        return {
            'name': 'galaxypron',
            'personality': 'helpful',
            'learning_enabled': True,
            'response_style': 'concise'
        }

    def _load_history(self):
        if self.history_file.exists():
            with open(self.history_file, 'r') as f:
                return json.load(f)
        return []

    def _save_history(self):
        with open(self.history_file, 'w') as f:
            json.dump(self.history, f, indent=2)

    def get_status(self):
        return {
            'name': self.config.get('name', 'galaxypron'),
            'active': True,
            'interactions': len(self.history),
            'context_size': len(self.context)
        }

    def get_config(self):
        return self.config

    def update_config(self, new_config):
        self.config.update(new_config)
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=2)

    def get_history(self, limit=50):
        return self.history[-limit:] if self.history else []

    def update_context(self, data):
        data_type = data.get('type', 'unknown')
        if data_type == 'navigation':
            self.context['current_url'] = data.get('url', '')
            self.context['current_title'] = data.get('title', '')
        elif data_type == 'tabs':
            self.context['open_tabs'] = data.get('tabs', [])
        elif data_type == 'activity':
            self.context['last_activity'] = data.get('activity', {})

    # ---- Helpers to pull real learning data ----
    def _learning_data(self):
        if self.learning_engine:
            return {
                'stats': self.learning_engine.get_stats(),
                'top_domains': self.learning_engine.get_learning_summary().get('top_domains', []),
                'preferences': self.learning_engine.get_learning_summary().get('preferences', {}),
                'recent_activity': self.learning_engine.get_learning_summary().get('recent_activity', []),
            }
        return {'stats': {}, 'top_domains': [], 'preferences': {}, 'recent_activity': []}

    def _model_stats(self):
        if self.training_engine:
            return self.training_engine.stats()
        return {}

    def _model_preferred(self, n=3):
        if self.training_engine:
            return self.training_engine.preferred_categories(n)
        return []

    def _top_categories(self, n=4):
        prefs = self._learning_data()['preferences']
        if not prefs:
            return []
        return sorted(prefs.items(), key=lambda x: x[1].get('count', 0), reverse=True)[:n]

    def _top_domains(self, n=5):
        return self._learning_data()['top_domains'][:n]

    # ---- Main entry ----
    def process_message(self, message, context=None):
        self.context.update(context or {})

        # Keep conversation memory (last 6 turns for context awareness)
        self.memory.append({'role': 'user', 'content': message})
        if len(self.memory) > 12:
            self.memory = self.memory[-12:]

        intent = self._classify_intent(message)
        response_text = self._generate_response(message, intent)

        self.memory.append({'role': 'assistant', 'content': response_text})

        interaction = {
            'user_message': message,
            'agent_response': response_text,
            'intent': intent,
            'timestamp': datetime.now().isoformat()
        }
        self.history.append(interaction)
        if len(self.history) > 1000:
            self.history = self.history[-1000:]
        self._save_history()

        return {'text': response_text, 'type': intent}

    # ---- Intent classification (rich) ----
    def _classify_intent(self, message):
        m = message.lower().strip()

        intent_tests = [
            ('greeting', ['hello', 'hi ', 'hi!', 'hey', 'greetings', 'good morning', 'good afternoon', 'good evening']),
            ('farewell', ['bye', 'goodbye', 'see you', 'later', 'exit', 'quit']),
            ('learned', ['what have you learned', 'what did you learn', 'what have you been learning', 'patterns have you', 'things have you learned', 'tell me what you learned']),
            ('top_sites', ['most visited', 'top sites', 'favorite sites', 'favorite websites', 'frequently visit', 'most common sites', 'what sites', 'websites do i', 'sites do i visit']),
            ('preferences', ['my interests', 'my preferences', 'what am i into', 'my likes', 'categories', 'interested in']),
            ('activity', ['recent activity', 'recent browsing', 'what did i do', 'what have i been browsing', 'my history', 'browsing history', 'recent sites']),
            ('suggest', ['suggest', 'recommend', 'suggestion', 'recommendation', 'what should i read', 'what should i watch', 'ideas for']),
            ('time_spent', ['how much time', 'time on', 'where do i spend', 'spend most time', 'screen time']),
            ('tabs', ['tabs', 'organize', 'close tabs', 'too many tabs']),
            ('help', ['help', 'what can you do', 'assist', 'how do you work', 'capabilities', 'features']),
            ('learning', ['learn', 'learning', 'pattern', 'trend', 'habit', 'smarter', 'train']),
            ('browser', ['browser', 'edge', 'website', 'current page', 'this page']),
            ('how_are_you', ['how are you', 'how are things', 'status', 'are you ok', 'you good']),
            ('thanks', ['thank', 'thx', 'appreciate']),
            ('gif', ['make a video', 'make video', 'create a video', 'generate video', 'generate a video', 'create video', 'animated gif', 'give me a video', 'show me a video']),
            ('img', ['generate an image', 'generate image', 'make an image', 'create an image', 'draw me', 'draw a picture', 'make a picture', 'generate a photo', 'give me an image', 'show me an image', 'make a photo']),
        ]

        for intent, keywords in intent_tests:
            for kw in keywords:
                if kw in m:
                    return intent

        if any(w in m for w in ['what', 'how', 'why', 'when', 'where', 'who']):
            return 'question'
        if any(w in m for w in ['can you', 'could you', 'please', 'would you']):
            return 'request'

        return 'general'

    # ---- Response generation ----
    def _generate_response(self, message, intent):
        handlers = {
            'greeting': self._handle_greeting,
            'farewell': self._handle_farewell,
            'help': self._handle_help,
            'learned': self._handle_learned,
            'top_sites': self._handle_top_sites,
            'preferences': self._handle_preferences,
            'activity': self._handle_activity,
            'suggest': self._handle_suggest,
            'tabs': self._handle_tabs,
            'time_spent': self._handle_time_spent,
            'learning': self._handle_learning,
            'browser': self._handle_browser,
            'how_are_you': self._handle_how_are_you,
            'thanks': self._handle_thanks,
            'question': lambda m: self._handle_question(m),
            'request': lambda m: self._handle_request(m),
            'gif': lambda m: self._handle_media(m, 'video'),
            'img': lambda m: self._handle_media(m, 'image'),
            'general': lambda m: self._handle_general(m),
        }
        handler = handlers.get(intent)
        return handler(message)

    def _handle_greeting(self, message):
        stats = self._learning_data()['stats']
        total_visits = stats.get('total_visits', 0)
        if total_visits > 0:
            return (f"Hey! I'm galaxypron. I've been learning from your browsing — I've tracked {total_visits:,} "
                    f"visits across {stats.get('unique_domains', 0)} sites so far. "
                    f"Ask me about your most-visited sites, your interests, or what I've learned about you.")
        return ("Hey! I'm galaxypron, your Edge AI agent. Right now I don't have any browsing data yet — "
                "install the Edge extension and browse around and I'll start learning. "
                "Try asking me 'what have you learned' after a bit.")

    def _handle_farewell(self, message):
        return "Goodbye! I'll keep learning from your browsing while you're away. Come back anytime — I remember our conversations."

    def _handle_help(self, message):
        return (
            "I'm galaxypron, an AI agent that learns from how you use Microsoft Edge. Here's what I can do:\n\n"
            "• **Learn** your browsing patterns and preferences\n"
            "• **Tell you** your most-visited sites and where you spend your time\n"
            "• **Suggest** websites and content based on your interests\n"
            "• **Analyze** your browsing activity and trends\n"
            "• **Help** with tabs and browsing habits\n\n"
            "Try asking:\n"
            "• \"What are my most visited sites?\"\n"
            "• \"What have you learned about me?\"\n"
            "• \"Suggest something based on my interests\"\n"
            "• \"Where do I spend the most time?\"\n"
            "• \"Generate an image of a sunset\"\n"
            "• \"Make a video of an ocean\""
        )

    def _handle_media(self, message, kind):
        if not self.media_generator:
            return ("I'd love to, but my image/video generator isn't connected. "
                    "This build runs on CPU, so I make simple procedural images and "
                    "animated GIFs (real AI generation needs a CUDA GPU).")
        prompt = self._extract_media_prompt(message)
        try:
            if kind == 'video':
                result = self.media_generator.generate_video(prompt=prompt)
                label = 'animated clip'
            else:
                result = self.media_generator.generate_image(prompt=prompt)
                label = 'image'
            url = '/generated/' + result['file']
            return (f"Here's your {label}: **{prompt or 'landscape'}**\n\n"
                    f"🖼️ {url}\n\n"
                    f"(Generated locally on CPU · {result.get('width','') or ''}"
                    f"{result.get('frames','') and str(result.get('frames')) + ' frames'} · "
                    f"{result.get('seconds','') and str(result.get('seconds')) + 's'})")
        except Exception as exc:
            return f"I ran into a problem generating that: {exc}"

    def _extract_media_prompt(self, message):
        # pull an extractable subject out of "generate an image of a sunset"
        low = message.lower()
        for kw in (' of ', ' about ', ' image of ', ' picture of ', ' video of '):
            idx = low.find(kw)
            if idx > 0:
                phrase = message[idx + len(kw):].strip(' ?!.').strip()
                if phrase and len(phrase) < 60:
                    return phrase
        return message.strip(' ?!.').strip()

    def _handle_learned(self, message):
        data = self._learning_data()
        stats = data['stats']
        if stats.get('total_visits', 0) == 0:
            return "I don't have any browsing data yet. Make sure the Edge extension is installed and active, then browse around — I'll start training my model and can tell you what I've picked up."

        lines = []
        lines.append(f"Here's what I've learned from your browsing so far:\n")
        lines.append(f"• **{stats.get('total_visits', 0):,} visits** across **{stats.get('unique_domains', 0)} different sites**")

        top = self._top_domains(3)
        if top:
            names = ", ".join(d['domain'] for d in top)
            lines.append(f"• Your **most-visited sites**: {names}")

        cats = self._top_categories(3)
        if cats:
            cat_str = ", ".join(f"{c} ({v['count']} visits)" for c, v in cats)
            lines.append(f"• Your **main interests**: {cat_str}")

        # Show what the neural network genuinely learned
        mstats = self._model_stats()
        if mstats.get('examples_seen', 0) > 0:
            lines.append(f"• My neural network has trained on **{mstats['examples_seen']} examples** "
                         f"over **{mstats['epochs']} epochs** (device: **{mstats.get('device')}**, "
                         f"loss {mstats['loss_history'][-1] if mstats.get('loss_history') else 'n/a'})")
            pref = self._model_preferred(3)
            if pref:
                model_cats = ", ".join(f"{p['category']} (confidence {p['engagement']})" for p in pref)
                lines.append(f"• My **model's learned preferences**: {model_cats}")

        recent = data.get('recent_activity', [])
        if recent:
            last = recent[-1]
            lines.append(f"• You were last on **{last.get('title') or last.get('url', 'a page')}**")

        lines.append("\nI train continuously on your browsing. Ask me to suggest something based on all this!")
        return "\n".join(lines)

    def _handle_top_sites(self, message):
        top = self._top_domains(5)
        if not top:
            return "I don't have enough browsing data yet to know your top sites. Keep browsing with the extension active and ask me again."
        
        lines = ["Here are your most-visited websites:\n"]
        for i, d in enumerate(top, 1):
            lines.append(f"{i}. **{d['domain']}** — {d['visits']} visits")
        
        total = self._learning_data()['stats'].get('total_visits', 1)
        lines.append(f"\nThese make up a big part of your {self._learning_data()['stats'].get('total_visits', 0):,} total visits.")
        return "\n".join(lines)

    def _handle_preferences(self, message):
        cats = self._top_categories(5)
        if not cats:
            return "I don't have enough data to determine your interests yet. Browse more and I'll learn what you're into."
        
        lines = ["Based on your browsing, you're most interested in:\n"]
        for cat, info in cats:
            domains = ", ".join(info.get('domains', [])[:3])
            lines.append(f"• **{cat.capitalize()}** ({info['count']} visits)" + (f" — e.g. {domains}" if domains else ""))
        
        lines.append("\nI use these interests to suggest content you'll probably enjoy.")
        return "\n".join(lines)

    def _handle_activity(self, message):
        recent = self._learning_data().get('recent_activity', [])
        if not recent:
            return "I don't have any recent activity recorded yet. Make sure the extension is running."
        
        lines = ["Here's your recent browsing activity:\n"]
        for v in recent[-5:][::-1]:
            ts = (v.get('timestamp') or '')[:16].replace('T', ' ')
            lines.append(f"• **{v.get('title') or v.get('url', 'a page')}** — {ts}")
        return "\n".join(lines)

    def _handle_suggest(self, message):
        data = self._learning_data()
        cats = self._top_categories(2)
        top = self._top_domains(1)
        
        if not cats and not top:
            return "I need browsing data to make good suggestions. Once I learn your interests, I'll recommend sites and content you'll actually like."
        
        suggestions = []
        for cat, info in cats:
            known = set(info.get('domains', []))
            cat_suggestions = {
                'news': ['Reuters', 'AP News', 'The Guardian'],
                'social': ['Reddit', 'X/Twitter', 'Discord'],
                'entertainment': ['YouTube', 'Spotify', 'Twitch'],
                'shopping': ['Amazon', 'eBay', 'Etsy'],
                'education': ['Coursera', 'edX', 'Khan Academy'],
                'tech': ['Hacker News', 'GitHub Trending', 'Stack Overflow'],
                'finance': ['Investing.com', 'Yahoo Finance', 'MarketWatch'],
                'general': ['Wikipedia', 'Medium', 'Quora']
            }
            for s in cat_suggestions.get(cat, []):
                if s.lower().replace(' ', '') not in known and s not in [c for c in suggestions]:
                    suggestions.append(s)
        
        lines = ["Based on your interests, here's what I'd recommend exploring:\n"]
        if suggestions:
            for s in suggestions[:4]:
                lines.append(f"• **{s}**")
        else:
            lines.append("• Keep diving deeper into the topics you already visit — you clearly enjoy them!")
        lines.append("\nWant me to suggest based on a specific interest? Just ask.")
        return "\n".join(lines)

    def _handle_tabs(self, message):
        tabs = self.context.get('open_tabs', [])
        if tabs:
            n = len(tabs)
            domains = set()
            for t in tabs:
                try:
                    domains.add(t['url'].split('/')[2])
                except Exception:
                    pass
            return (f"I can see you have **{n} tabs open** across {len(domains)} sites. "
                    f"Try closing tabs you're not using to keep things focused. "
                    f"If you want, I can help you find the sites you visit most so you can decide what matters.")
        return "I don't currently see your open tabs. Keep the extension running and I'll be able to help organize them."

    def _handle_time_spent(self, message):
        stats = self._learning_data()['stats']
        top = self._top_domains(3)
        if not top:
            return "I don't have time data yet. Keep browsing with the extension and I'll analyze where your time goes."
        
        total = sum(d['visits'] for d in top)
        lines = ["Here's where your browsing attention goes:\n"]
        for d in top:
            pct = round((d['visits'] / max(total, 1)) * 100)
            lines.append(f"• **{d['domain']}** — {d['visits']} visits (~{pct}% of your top sites)")
        lines.append("\nI use visit frequency as a proxy for time since it reflects your attention.")
        return "\n".join(lines)

    def _handle_learning(self, message):
        return ("I learn continuously from how you use Edge — the sites you visit, how often, and what you engage with. "
                "To make the most of me: keep the extension on, browse regularly, and ask me questions about your habits. "
                "The more data I get, the sharper my recommendations become.")

    def _handle_browser(self, message):
        url = self.context.get('current_url')
        title = self.context.get('current_title')
        if url:
            return f"You're currently on **{title or 'this page'}**\n({url})\n\nWant me to dig into this site or suggest something similar?"
        return "I'm not seeing which page you're on right now. Open a tab with the extension active and I'll track it."

    def _handle_how_are_you(self, message):
        stats = self._learning_data()['stats']
        data = self._learning_data()
        if stats.get('total_visits', 0) > 0:
            return f"I'm doing great! I've learned from {stats.get('total_visits', 0):,} browsing visits and I'm ready to help. What can I do for you?"
        return "I'm doing great, just getting warmed up! I haven't learned anything about your habits yet. Browse around and I'll start figuring you out."

    def _handle_thanks(self, message):
        return "You're welcome! Happy to help. Let me know if you want deeper insights or suggestions."

    def _handle_question(self, message):
        # Try to answer factual/reasoning questions with what we know; otherwise engage meaningfully
        if 'most visited' in message or 'favorite' in message:
            return self._handle_top_sites(message)
        if 'interest' in message or 'preference' in message or 'into' in message:
            return self._handle_preferences(message)
        if 'spend' in message or 'time' in message:
            return self._handle_time_spent(message)
        if 'learn' in message:
            return self._handle_learned(message)

        # General-knowledge question ("what is a car?", "who is Einstein?"): answer
        # with a grammar-correct Wikipedia extract so the reply is logical + factual.
        kb_answer = self._wikipedia_answer(message)
        if kb_answer:
            return kb_answer

        # Not found locally: research it from the web and compose a full answer.
        web_answer = self._web_research_answer(message)
        if web_answer:
            return web_answer

        # Reasonable generic but non-dismissive fallback
        return ("Good question. I can answer things about your browsing habits and help find content you'll like. "
                "If you're asking about something specific, try asking me about your top sites, interests, or what I've learned — "
                "I'll give you a real answer from your data.")

    def _wikipedia_answer(self, message):
        """Fetch the topic's Wikipedia article and return a long, detailed,
        grammatically-correct and easy-to-read answer (like explaining to
        Einstein)."""
        topic = self._extract_topic(message)
        if not topic:
            return None
        return self._wikipedia_topic_answer(
            topic, parts=self._is_parts_question(message))

    def _wikipedia_topic_answer(self, topic, parts=False):
        """Answer about a topic via Wikipedia, search-first so fuzzy wording
        still finds the right article.

        Parts questions route to curated articles (e.g. "pc" -> "Computer
        hardware") because Wikipedia's search ranks shopping sites and
        disambiguation pages too high for short queries like "pc components".
        """
        obj = re.sub(r'^(a|an|the)\s+', '', topic.lower()).strip()

        # 1) curated article: the most reliable, best-fitting source
        if parts and obj in self.PARTS_ARTICLE:
            ans = self._fetch_extract(self.PARTS_ARTICLE[obj], parts=True)
            if ans:
                return ans
        if not parts and obj in self.SYNONYM_ARTICLE:
            ans = self._fetch_extract(self.SYNONYM_ARTICLE[obj])
            if ans:
                return ans

        # 2) search-based fallback for anything not curated
        query_variants = ([f'{obj} parts', f'{obj} components', obj]
                          if parts else [obj])
        for q in query_variants:
            title = self._search_wiki_title(q)
            if not title:
                continue
            ans = self._fetch_extract(title, parts=parts)
            if ans:
                return ans
        return None

    def _search_wiki_title(self, query):
        """Return the best Wikipedia article title for a query, rejecting
        obvious junk (shopping sites, list/portal pages) that pollutes top
        results for short, generic queries."""
        data = self._wiki_get({
            'action': 'query', 'format': 'json',
            'list': 'search', 'srsearch': query, 'srlimit': 4,
        })
        if not data:
            return None
        hits = (data.get('query', {}).get('search', []) or [])
        junk = re.compile(r'picker|shopping|buy|price|market\b|retailer'
                          r'|cookware|kitchen|comparison|portal|wikimedia'
                          r'|category:', re.IGNORECASE)
        for h in hits:
            t = h.get('title', '')
            if t and not junk.search(t):
                return t
        return None

    def _wikitext_to_text(self, raw):
        """Strip MediaWiki markup off a lead-section wikitext string."""
        if not raw:
            return ''
        s = raw[:6000]
        s = re.sub(r'<ref[^>]*>.*?</ref>', ' ', s, flags=re.S | re.I)
        s = re.sub(r'<ref[^>]*/>', ' ', s, flags=re.I)
        s = re.sub(r'<[^>]+>', ' ', s)
        for _ in range(6):
            s = re.sub(r'\{\{[^{}]*\}\}', ' ', s)
        s = re.sub(r'\{\|[^{}]*\|}', ' ', s, flags=re.S)
        s = re.sub(r'(?m)^\s*\[\[File:.*\]\]\s*$', ' ', s)
        s = re.sub(r'\[\[(?:File|Image|Category|Template|Media|Portal):(?:[^\]]|\]\])*\]\]', ' ', s, flags=re.S)
        s = re.sub(r'\[\[[^\]|]*\|([^\]]*)\]\]', r'\1', s)
        s = re.sub(r'\[\[([^\]]*)\]\]', r'\1', s)
        s = re.sub(r'\[https?://[^\]\s]+\s+([^\]]+)\]', r'\1', s)
        s = re.sub(r'\[https?://[^\]]*\]', ' ', s)
        s = re.sub(r"'{2,}", '', s)
        s = re.sub(r'[=]+', ' ', s)
        s = re.sub(r'[\[\]{}|]', ' ', s)
        s = ' '.join(s.split())
        return s

    def _fetch_extract(self, fetch_topic, parts=False):
        """Answer from a Wikipedia article, revisions-first for reliability.

        The plain-text (prop=extracts) endpoint is heavily rate-limited by
        Wikimedia, which broke answers whenever the 24/7 trainer was running.
        The raw revisions endpoint (rvsection=0) is what the trainer uses at
        full speed and is rarely throttled, so we fetch the lead wikitext via
        revisions and strip markup locally, with the extract API as fallback.
        """
        data = self._wiki_get({
            'action': 'query', 'format': 'json',
            'prop': 'revisions', 'rvprop': 'content',
            'rvslots': 'main', 'rvsection': 0,
            'redirects': 1, 'titles': fetch_topic,
        }, tries=3)
        if data:
            pages = data.get('query', {}).get('pages', {}) or {}
            page = next(iter(pages.values()), {})
            title = page.get('title') or fetch_topic
            raw = ''
            for rev in (page.get('revisions') or []):
                slots = rev.get('slots') or {}
                if slots:
                    main = slots.get('main') or {}
                    raw = main.get('*') or main.get('content') or ''
                else:
                    raw = rev.get('*') or ''
                if raw:
                    break
            text = self._wikitext_to_text(raw) if raw else ''
            # drop first-sentence disambiguation/template line
            text = re.sub(r'(?:Look up\s*|For\s+[^.]*see\s+|may refer to\b).*?\.', ' ', text[:2500])
            if text.strip():
                clean = re.sub(r'\s+', ' ', text).strip()
                sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean)]
                chosen = self._pick_readable(sents, title, fetch_topic, parts=parts)
                body = '\n\n'.join(chosen[:3]) if chosen else clean[:800]
                return f"**{title}**\n\n{body}"

        # fallback: the (rate-limitable) plain-text extract endpoint
        data = self._wiki_get({
            'action': 'query', 'format': 'json', 'prop': 'extracts',
            'explaintext': 1, 'redirects': 1, 'titles': fetch_topic,
        })
        if data:
            pages = data.get('query', {}).get('pages', {}) or {}
            page = next(iter(pages.values()), {})
            extract = (page.get('extract') or '').strip()
            title = page.get('title') or fetch_topic
            if extract:
                clean = self._clean_text(extract)
                clean = re.sub(r'\[ *edit *\]|\{\{.*?\}\}|\|(?!\S)|\bmay refer to\b', ' ', clean)
                clean = re.sub(r'\s+', ' ', clean).strip()
                sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean)]
                chosen = self._pick_readable(sents, title, fetch_topic, parts=parts)
                body = '\n\n'.join(chosen[:3]) if chosen else clean[:800]
                return f"**{title}**\n\n{body}"
        return None

    def _pick_readable(self, sentences, title, topic, parts=False):
        """Choose the most definitional, readable sentences for a long answer."""
        key = set(re.findall(r"[a-zA-Z']+", (title + ' ' + topic).lower()))
        bad = re.compile(r'\{|\}|\[ *edit *\]|:\s*$|\bNOTE:|cite|u can |\bor\b-|^\s*[A-Z][a-z]+ - ')
        good = []
        for s in sentences:
            if not (12 < len(s) < 420):
                continue
            if bad.search(s):
                continue
            # skip stuff that parses like wiki templates / references
            if re.search(r'\{\{|\[\[|\]\]|\|cite|\bdoi\b|\bhref\b', s):
                continue
            lw = s.lower()
            # keep sentences that define or clearly relate to the topic
            has_word = any(w in lw for w in key if len(w) > 3)
            if not has_word and not good:
                continue
            # de-jargon: penalise only heavy parenthetical lists
            parens = s.count('(') + s.count(')')
            if parens > 6:
                continue
            good.append(s)
        # for composition questions, surface the sentence that enumerates parts
        if parts:
            comp = ('parts', 'components', 'including', 'includes', 'include',
                    'such as', 'consists', 'composed', 'made of', 'contain',
                    'contains', 'built from')
            good.sort(key=lambda s: sum(1 for w in comp if w in s.lower()),
                      reverse=True)
        # ensure a strong opener: first sentence that actually defines the thing
        if good and not any(w in good[0].lower() for w in key if len(w) > 3):
            for i, s in enumerate(good[1:], 1):
                if any(w in s.lower() for w in key if len(w) > 3):
                    good[0], good[i] = good[i], good[0]
                    break
        return good[:3] if len(good) >= 3 else (good if good else [])

    def _clean_text(self, raw):
        """Strip HTML tags, wiki markup and collapse whitespace into prose."""
        if not raw:
            return ''
        text = re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>', ' ', raw)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        # remove wiki/template/reference artifacts
        text = re.sub(r'\{\{[\s\S]*?\}\}', ' ', text)
        text = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', text)
        text = re.sub(r'\[ *edit *\]', ' ', text)
        text = re.sub(r'\[\s*\d+\s*\]', ' ', text)   # [ 59 ] reference markers
        # drop bare wiki section headers (e.g. "== Physical geography ==")
        text = re.sub(r'={2,}\s*[^=]{1,60}?\s*={2,}', ' ', text)
        text = text.replace('{{', ' ').replace('}}', ' ')
        text = re.sub(r'\|cite\b[^ ]*', ' ', text)
        text = re.sub(r'\b(doi|href|png|jpg|jpeg|svg)\b', ' ', text)
        # drop trailing nav/reference footers
        for marker in ['External links', 'Media related to', 'References',
                       'Categories:', 'See also', 'Further reading']:
            idx = text.find(marker)
            if idx > 0 and idx > len(text) * 0.5:
                text = text[:idx]
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _url_blocked(self, url):
        """True if a scraped URL should never be used (adult/gambling sites)."""
        low = (url or '').lower()
        return any(frag in low for frag in self.BLOCKED_DOMAIN_FRAGS)

    def _text_unsafe(self, text):
        """True if scraped text contains explicit adult/gambling language."""
        low = (text or '').lower()
        return any(w in low for w in self.EXPLICIT_WORDS)

    def _wiki_get(self, params, tries=3):
        """Paced, retrying Wikipedia API GET returning parsed JSON or None.

        Serialises all agent-side Wikipedia calls through a shared lock with a
        small gap between them and exponential back-off on 429s — this stops
        rapid-fire question bursts from tripping Wikimedia's throttle and
        degrading into web-scraped garbage answers.
        """
        with AIAgent._WIKI_LOCK:
            for attempt in range(tries):
                gap = AIAgent._WIKI_PACE - (time.time() - AIAgent._LAST_WIKI)
                if gap > 0:
                    time.sleep(gap)
                AIAgent._LAST_WIKI = time.time()
                try:
                    resp = requests.get(
                        'https://en.wikipedia.org/w/api.php', params=params,
                        timeout=15, headers={'User-Agent': 'galaxypron/1.0'})
                except Exception:
                    time.sleep(1 + attempt)
                    continue
                if resp.status_code == 429:
                    # Rate-limited (the 24/7 trainer constantly hammers the
                    # same API). Wait a *small* amount then move on — long
                    # back-offs make questions feel dead when the answer path
                    # merely falls through to web research.
                    time.sleep(2 + attempt)
                    continue
                if resp.status_code != 200:
                    return None
                try:
                    return resp.json()
                except Exception:
                    time.sleep(1 + attempt)
        return None

    def _web_research_answer(self, message):
        """When the topic isn't found locally, research it from the web:
        read a couple of pages, pick the most relevant sentences and compose a
        full, grammatically-correct answer."""
        topic = self._extract_topic(message)
        if not topic:
            return None

        try:
            # First try Wikipedia's own search so we reuse the richest reliable
            # source ("is it in the database?" -> search the knowledge source).
            sresp = self._wiki_get({
                'action': 'query', 'format': 'json', 'list': 'search',
                'srsearch': topic, 'srlimit': 1, 'redirects': 1,
            })
            if sresp:
                hits = (sresp.get('query', {}).get('search', []) or [])
                if hits:
                    best = hits[0]['title']
                    # recurse into the extract path for a clean full answer
                    parts = self._is_parts_question(message)
                    direct = self._wikipedia_topic_answer(best, parts=parts)
                    if direct:
                        return direct
        except Exception:
            pass

        # Fallback: web search -> fetch a couple of pages -> extract sentences.
        try:
            urls = self._search_urls(topic)
            if not urls:
                return None
            sentences = []
            for u in urls[:2]:
                if self._url_blocked(u):
                    continue
                try:
                    pg = requests.get(u, timeout=15,
                                      headers={'User-Agent': 'Mozilla/5.0'})
                    if pg.status_code != 200:
                        continue
                    text = self._clean_text(pg.text)
                    if self._text_unsafe(text):
                        continue
                    sentences.extend(self._relevant_sentences(text, topic))
                except Exception:
                    continue
            composed = self._compose_answer(sentences, topic)
            if composed:
                return composed
        except Exception:
            return None
        return None

    def _search_urls(self, topic, limit=2):
        """Return real page URLs relevant to the topic (Bing primary, Mojeek fallback)."""
        ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        urls = []
        # Bing: results are base64-encoded in the u= param of bing.com/ck/a links
        try:
            r = requests.get('https://www.bing.com/search', params={'q': topic},
                             timeout=15, headers={'User-Agent': ua})
            if r.status_code == 200:
                for raw in re.findall(r'<h2[^>]*><a[^>]+href="([^"]+)"', r.text):
                    clean = html.unescape(raw)
                    mm = re.search(r'[?&]u=a1([A-Za-z0-9+/=]+)', clean)
                    if mm:
                        try:
                            url = base64.b64decode(mm.group(1) + '==').decode('utf-8', 'ignore')
                            if (url.startswith('http') and url not in urls
                                    and not self._url_blocked(url)):
                                urls.append(url)
                        except Exception:
                            continue
                    if len(urls) >= limit:
                        return urls
        except Exception:
            pass
        # DuckDuckGo html fallback (condensed results, tolerant of scrapers)
        try:
            from urllib.parse import unquote
            r = requests.get('https://html.duckduckgo.com/html/', params={'q': topic},
                             timeout=15, headers={'User-Agent': ua})
            if r.status_code == 200:
                for raw in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
                    href = html.unescape(raw)
                    mm = re.search(r'uddg=([^&]+)', href)
                    if mm:
                        href = unquote(mm.group(1))
                    if (href.startswith('http') and href not in urls
                            and not self._url_blocked(href)):
                        urls.append(href)
                    if len(urls) >= limit:
                        return urls[:limit]
        except Exception:
            pass
        # Mojeek fallback (plain relative hrefs)
        try:
            r = requests.get('https://www.mojeek.com/search', params={'q': topic},
                             timeout=15, headers={'User-Agent': ua})
            if r.status_code == 200:
                for raw in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', r.text):
                    if (raw not in urls and 'mojeek.com' not in raw
                            and not self._url_blocked(raw)):
                        urls.append(raw)
                    if len(urls) >= limit:
                        break
        except Exception:
            pass
        return urls[:limit]

    def _relevant_sentences(self, text, topic):
        """Pull clean, topic-relevant, self-contained sentences from a page."""
        topic_words = set(re.findall(r"[a-zA-Z']+", topic.lower()))
        # drop wiki/template/reference fragments and messy lines
        junk = re.compile(r'\{\{|\[\[|\]\]|\|cite\b|\bdoi\b|\bhref\b|\[ *edit *\]|:\s*$|^\s*http'
                          r'|^\s*\[', re.IGNORECASE)
        sentences = re.split(r'(?<=[.!?])\s+', text)
        scored = []
        for s in sentences:
            s = s.strip()
            if not (12 < len(s) < 340):
                continue
            if junk.search(s):
                continue
            if s.count('(') + s.count(')') > 2:
                continue
            hits = sum(1 for tw in topic_words if tw in s.lower())
            if hits >= max(1, len(topic_words) // 2):
                scored.append((hits, len(s), s))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [s for _, _, s in scored[:5]]

    def _compose_answer(self, sentences, topic):
        """Compose a long, grammatical, easy-to-read multi-sentence answer."""
        if not sentences:
            return None
        seen = set()
        uniq = []
        for s in sentences:
            if self._text_unsafe(s):
                continue
            key = s[:40]
            if key not in seen:
                seen.add(key)
                uniq.append(s.rstrip('.') + '.')
            if len(uniq) >= 4:
                break
        if not uniq:
            return None
        t = topic.strip()
        head = f"**{t[:1].upper() + t[1:]}**\n\n"
        return head + '\n\n'.join(uniq)

    def _extract_topic(self, message):
        """Pull a searchable topic out of a natural-language question."""
        m = message.strip()
        # direct "What is X?" / "Who is X?" / "Define X"
        patterns = [
            # ---- composition / parts questions ----
            r"^\s*what\s+parts?\s+(?:does|do)\s+(.+?)\s+consist\s+of\s*[?.]*$",
            r"^\s*what\s+parts?\s+(?:does|do|is|are|can|would)\s+(.+?)\s+(?:have|contain|include|comprise|make\s+up)\s*[?.]*$",
            r"^\s*(?:from|of)\s+what\s+parts?\s+(?:consists?|does\s+consist|is|are|were|do\s+we\s+make|do\s+people\s+make)\s*(?:of\s+)?(.+?)\s*[?.]*$",
            r"^\s*what\s+parts?\s+(?:make|makes)\s+up\s+(.+?)\s*[?.]*$",
            r"^\s*what\s+(?:are\s+)?(?:the\s+)?(?:parts|components|pieces)\s+(?:of|in|inside|that\s+make\s+up|which\s+make\s+up)\s+(.+?)\s*[?.]*$",
            r"^\s*what\s+does\s+(.+?)\s+(?:consist|comprise|include)\s*(?:of\s*|with\s*)?[?.]*$",
            r"^\s*what\s+is\s+(.+?)\s+(?:made\s+of|made\s+from|composed\s+of|made\s+up\s+of|made\s+out\s+of|built\s+from|built\s+of|build\s+from)\s*[?.]*$",
            r"^\s*(?:from|of)\s+what\s+(?:parts?|components?|materials?)\s+is\s+(.+?)\s+(?:made|built|composed)\s*[?.]*$",
            r"^\s*(?:of|from)\s+what\s+(?:is|are|was|were)\s+(.+?)\s+(?:made|built|composed)\s*[?.]*$",
            r"^\s*how\s+many\s+(?:parts|components)\s+(?:does\s+)?(.+?)\s+(?:have|has)\s*[?.]*$",
            # ---- generic “what is X” ----
            r"^\s*(?:what|which)\s+(?:is|are|was|were)\s+(?:the\s+|an?\s+)?(.+?)\s*[?.]*$",
            r"^\s*(?:who|what)\s+was\s+(?:the\s+)?(.+?)\s*[?.]*$",
            r"^\s*define\s+(.+?)\s*[?.]*$",
            r"^\s*(?:tell\s+me\s+about|explain|what\s+about)\s+(?:the\s+|an?\s+)?(.+?)\s*[?.]*$",
            r"^\s*what\s+does\s+(.+?)\s+mean\s*[?.]*$",
            r"^\s*where\s+(?:is|are|was|were|does)\s+(?:the\s+|a\s+|an\s+)?(.+?)\s*(?:located\s*)?[?.]*$",
            r"^\s*where\s+can\s+i\s+find\s+(?:the\s+|a\s+)?(.+?)\s*[?.]*$",
            r"^\s*where\s+(.+?)\s+is\s+located\s*[?.]*$",
            r"^\s*where\s+(.+?)\s+is\s*[?.]*$",
            r"^\s*where\s+(.+?)\s+(?:located|situated)\s*[?.]*$",
        ]
        for pat in patterns:
            mm = re.match(pat, m.lower())
            if mm:
                t = re.sub(r"\s+", " ", mm.group(1)).strip().strip('"\'')
                # drop trailing filler words
                t = re.sub(r"\b(please|thanks|thank you)\b.*$", "", t).strip()
                return t if t else None
        return None

    def _is_parts_question(self, message):
        """True for composition questions: “what parts does X have / X consist of /
        what is X made of / parts of X” etc."""
        m = message.lower()
        markers = ('what parts', 'which parts', 'parts does', 'parts is', 'parts are',
                   'parts make up', 'parts consist', 'parts of', 'components of',
                   'components in', 'consist', 'consists', 'comprise', 'comprises',
                   'composed of', 'made of', 'made from', 'made up of', 'made out of',
                   'make up', 'built from', 'built of', 'inside of', 'made with')
        if any(mk in m for mk in markers):
            return True
        # "of what is X made" / "from what is X built" — verb comes last
        if re.match(r'^\s*(?:of|from)\s+what\b', m) and re.search(r'\b(made|built|composed)\b', m):
            return True
        return False

    def _handle_request(self, message):
        if 'suggest' in message or 'recommend' in message:
            return self._handle_suggest(message)
        if 'organize' in message or 'tab' in message:
            return self._handle_tabs(message)
        if 'tell me' in message or 'what' in message or 'show' in message:
            return self._handle_learned(message)
        return ("I'd be glad to help with that. I can analyze your browsing, suggest sites, or summarize your habits. "
                "Tell me a bit more about what you'd like and I'll put my knowledge of your preferences to work.")

    def _handle_general(self, message):
        # Meaningful, diverse responses that relate to what the agent does
        if 'edge' in message.lower() or 'extension' in message.lower():
            return ("I work through a Microsoft Edge extension that tracks how you browse. "
                    "That data is what powers my suggestions and insights. Make sure it's loaded in "
                    "edge://extensions so I keep learning.")
        # Try to answer factual/general-knowledge queries via Wikipedia
        kb_answer = self._wikipedia_answer(message)
        if kb_answer:
            return kb_answer
        # Not found locally: research it from the web and build a full answer.
        web_answer = self._web_research_answer(message)
        if web_answer:
            return web_answer
        return ("Got it. I'm here to make your browsing smarter — I can analyze your habits, "
                "suggest sites you'll love, or tell you what I've learned about you. "
                "What would you like to explore?")
