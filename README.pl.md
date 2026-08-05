# InfoCar: sprawdzanie i automatyczna rezerwacja terminów egzaminu

Wersja angielska: [README.md](README.md)

Bot w Pythonie, który obserwuje [info-kierowca.pl](https://info-kierowca.pl) w poszukiwaniu wolnych terminów egzaminu praktycznego, wysyła wiadomość na Telegramie w chwili, gdy termin się pojawi, i potrafi go za Ciebie zarezerwować.

To darmowy odpowiednik zlap-termin. Wystarczy Python i jedna komenda.

## Komenda

```bash
pip install requests playwright
playwright install chromium

python infocar_bot.py --org-id 43 --days 14 --auto-book
```

Zamień 43 na ID swojego ośrodka, zanim uruchomisz tę komendę. 43 to PORD Gdańsk, a `--auto-book` naprawdę zarezerwuje tam termin. Swoje ID znajdziesz przez `--list-words`. Za pierwszym razem możesz też pominąć `--auto-book`, żeby najpierw zobaczyć bota w działaniu, zanim cokolwiek zarezerwuje.

Przy pierwszym uruchomieniu otworzy się okno przeglądarki, żeby zalogować się przez login.gov.pl albo aplikację eDO App. Sesja zapisuje się w katalogu `./browser_data`, a bot od razu przechodzi do pętli monitorowania. Kolejne uruchomienia pomijają okno logowania.

Jeśli wolisz zalogować się wcześniej, uruchom osobno `python infocar_bot.py --login`.

## Jak bot utrzymuje sesję

To była najtrudniejsza część, więc warto wiedzieć, jak działa.

Serwis wydaje token JWT ważny 900 sekund. Bot wywołuje `/bknd/auth/api/v1/jwt/refresh` na początku każdego cyklu i nigdy nie śpi dłużej niż 600 sekund, więc token jest odnawiany, zanim wygaśnie. Ciasteczka trzymane są w sesji biblioteki requests, a nie w stałym nagłówku, ponieważ serwer rotuje `__Secure-PUDOJT` przy każdym odświeżeniu.

W trakcie pracy bota żadna przeglądarka nie pozostaje otwarta. Frontend serwisu sam wywołuje `jwt/logout` po 600 sekundach bez ruchu myszy lub klawiatury, a to wylogowanie działa po stronie serwera, nie tylko w karcie.

Stąd jedna zasada, którą warto zapamiętać: nie zostawiaj otwartej strony info-kierowca.pl we własnej przeglądarce, kiedy bot pracuje. Taka bezczynna karta wyloguje bota po około dziesięciu minutach.

## Limit jednej godziny

Odświeżanie nie działa w nieskończoność. Serwer ogranicza sesję do 3600 sekund od logowania, a potem odpowiada na `jwt/refresh` kodem HTTP 500, niezależnie od tego, co robi klient. Nie da się tego obejść: 500 utrzymuje się mimo ponownych prób i restartu procesu, a token nie zawiera żadnej informacji, która pozwoliłaby botowi przewidzieć ten moment. Pomaga wyłącznie nowe logowanie, a do tego potrzebujesz siebie i telefonu.

Dlatego bot traktuje nieudane odświeżenie jako pytanie, a nie wyrok. Sprawdza API i jeśli sesja nadal odpowiada, pracuje dalej na tokenie, który już ma, aż ten faktycznie wygaśnie, co daje jeszcze około dziesięciu minut monitorowania. Pięć minut przed limitem dostajesz wiadomość na Telegramie, żebyś mógł podstawić świeżą sesję, zamiast po godzinie odkryć martwego bota. Zrób to w tej kolejności: zatrzymaj bota, uruchom `--login`, włącz go ponownie. Działający bot nadpisuje `auth_token` przy każdym udanym odświeżeniu, więc logowanie obok niego skończy się nadpisaniem nowego tokenu starym. Czas logowania zapisywany jest w `config.json` jako `session_started_at`, dzięki czemu ostrzeżenie przetrwa restart.

Pojedyncze zerwanie połączenia również nie kończy już pracy bota. Odświeżenie ponawiane jest trzy razy z krótkim opóźnieniem, a za odmowę wartą zakończenia pracy uznawany jest tylko jednoznaczny kod 401 lub 404 z bramki.

## Jak wygląda pojedyncze sprawdzenie

Co 4 do 6 minut (losowo, ale nigdy z przerwą dłuższą niż 10 minut) bot odświeża sesję, a potem przechodzi przez Twoje okno wyszukiwania w blokach po 7 dni, z przerwą od 5 do 15 sekund między zapytaniami. Terminy są odfiltrowywane z duplikatów po dacie i identyfikatorze egzaminu, sortowane, a wygrywa najwcześniejszy.

Z opcją `--auto-book` bot tworzy rezerwację, otwiera strumień potwierdzenia, a następnie do czterech razy sprawdza jej stan. Za zarezerwowany uznaje wyłącznie stan `PlaceReserved` lub `Accepted`. Każdy inny wynik trafia na Telegram jako niepowodzenie z prośbą o ręczną rezerwację, bo rezerwacja, która utknęła w stanie `Created`, jeszcze nie jest Twoja.

Bez `--auto-book` dostajesz samo powiadomienie i rezerwujesz ręcznie.

## Komendy

```bash
python infocar_bot.py --login                          # jednorazowe logowanie i zapis sesji
python infocar_bot.py --test-telegram                   # wysyła wiadomość testową
python infocar_bot.py --list-words                      # lista ośrodków WORD i ich ID
python infocar_bot.py --check-once                      # jedno sprawdzenie i wyjście
python infocar_bot.py --org-id 43 --days 14             # monitorowanie, tylko powiadomienia
python infocar_bot.py --org-id 43 --days 14 --auto-book # monitorowanie z rezerwacją
python infocar_bot.py --all-words --days 14             # monitorowanie wszystkich znanych ośrodków WORD
```

`--list-words` czyta plik `word_centers.json`. Jeśli tego pliku nie ma, lista będzie pusta, ale dowolne całkowite ID organizacji i tak zadziała. 43 to PORD Gdańsk.

`--all-words` (`-a`) sprawdza wszystkie ośrodki z `word_centers.json` (na dziś 91) zamiast `--org-id`. Wymaga obecności `word_centers.json` i nadpisuje `--org-id` oraz `organization_id(s)` z `config.json`, gdy jest ustawiona. Każdy cykl sprawdzania będzie wtedy znacznie wolniejszy, bo przechodzi przez wszystkie te ośrodki zamiast jednego.

`easy_word_centers.json` to ręcznie wybrana lista ośrodków WORD z historycznie wyższą zdawalnością egzaminu praktycznego, przydatna, jeśli przy wyborze `--org-id` chcesz kierować się szansą na zdanie, a nie tylko bliskością. To sam plik referencyjny, bot go automatycznie nie odczytuje.

## Konfiguracja

Skopiuj `config.example.json` do `config.json` i uzupełnij:

| Klucz | Do czego służy |
| --- | --- |
| `auth_token` | Ciasteczka sesji. Bot zapisuje je sam po zalogowaniu, więc nie ruszaj tego pola. |
| `pkk` | Twój numer profilu PKK. Jeśli go pominiesz, bot pobierze go z konta. |
| `organization_id` | ID ośrodka WORD, na przykład 43. |
| `organization_ids` | Kilka ID ośrodków WORD do sprawdzania w każdym cyklu, np. `[43, 42, 73]`. Ma pierwszeństwo przed `organization_id`, jeśli oba są ustawione. `--org-id 43,42,73` robi to samo z linii poleceń. |
| `max_days` | Na ile dni do przodu szukać. |
| `auto_book` | `true` dla automatycznej rezerwacji, `false` dla samych powiadomień. |
| `min_interval` / `max_interval` | Zakres przerwy między sprawdzeniami, w sekundach. Domyślnie 240 i 360, a wszystko powyżej 600 zostaje przycięte, żeby chronić token JWT. |
| `telegram_bot_token` | Od @BotFather. |
| `telegram_chat_id` | Twoje ID czatu. |

Telegram jest opcjonalny. Bez tokenu i ID czatu bot zapisuje w logu to, co by wysłał, i pracuje dalej.
