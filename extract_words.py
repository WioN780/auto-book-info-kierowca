import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('info-kierowca-fetch.pl.har', 'r', encoding='utf-8', errors='ignore') as f:
    data = json.load(f)

words_dict = {}
for entry in data['log']['entries']:
    if 'dict/words' in entry['request']['url']:
        text = entry['response']['content'].get('text', '[]')
        words = json.loads(text)
        for w in words:
            words_dict[str(w['id'])] = f"{w.get('name')} ({w.get('location')})"

print(f"Extracted {len(words_dict)} WORD centers.")
with open('word_centers.json', 'w', encoding='utf-8') as out:
    json.dump(words_dict, out, ensure_ascii=False, indent=2)
