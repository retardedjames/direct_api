"""
Template for web_cookie.py (gitignored). Copy this file to web_cookie.py
and paste in a logged-in www.tiktok.com cookie + matching User-Agent.

How to grab them:
  1. Log in at https://www.tiktok.com in any normal browser.
  2. DevTools → Network → reload the page.
  3. Click any request to www.tiktok.com → Headers tab.
  4. Copy the entire `cookie:` request header value.
  5. Copy the `user-agent:` request header value.

The cookie's `sid_guard` is the load-bearing piece (account session). The
`msToken` value at the end gets refreshed by the browser on every nav;
scrape_keyword_web.py extracts whatever the latest msToken is from this
cookie string at request time. If scraping starts silent-failing, regrab
the cookie — msToken can rotate within hours.
"""

COOKIE = "ttwid=...; sid_guard=...; sessionid=...; msToken=..."

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
