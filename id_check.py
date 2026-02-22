import re

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()

html = content[content.find('DASHBOARD_HTML'):]

hids = set(re.findall(r'id=["\']([^"\']+)["\']', html))
jids = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', html))

si = sorted([i for i in jids if 'scan' in i.lower()])
sh = sorted([i for i in hids if 'scan' in i.lower()])

print("JS refs:", si)
print("HTML ids:", sh)
missing = [i for i in si if i not in hids]
print("Missing:", missing if missing else "None")
