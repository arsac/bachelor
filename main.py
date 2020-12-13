import argparse
import json
import queue
import threading
import time
from datetime import datetime, timedelta
from urllib import parse
from urllib.error import HTTPError
from urllib.request import build_opener, install_opener, Request, urlopen

parser = argparse.ArgumentParser()

parser.add_argument("-l", "--plates", help="License Plate")
parser.add_argument("-e", "--email", help="Email")
parser.add_argument("-p", "--password", help="Password")
parser.add_argument("-d", "--dates", help="Dates")
parser.add_argument("--dry-run", help="Dry run", action='store_true')
parser.add_argument('--interval', nargs='?', const=1, type=int, default=3)

args = parser.parse_args()

API_BASE = 'https://api.parkwhiz.com/v4/'

BACHELOR_VENUE_ID = 478498


def set_auth_token():
    print(f'Attempting to login user: {args.email}')
    payload = {
        'scope': 'public',
        'grant_type': 'password',
        'customer_email': args.email,
        'customer_password': args.password,
    }

    req = Request(url=f'{API_BASE}/oauth/token', method='POST', data=parse.urlencode(payload).encode())
    with urlopen(req) as res:
        data = json.loads(res.read().decode())
        opener = build_opener()
        opener.addheaders = [('Authorization', 'Bearer ' + data['access_token'])]
        install_opener(opener)


set_auth_token()


def api_get(path, params={}):
    query_string = parse.urlencode(params)
    req = Request(url=f'{API_BASE}/{path}?{query_string}', method='GET')
    return api_call(req)


def api_post(path, data):
    req = Request(url=f'{API_BASE}/{path}', method='POST', data=parse.urlencode(data).encode())
    return api_call(req)


def api_call(req, tries=1):
    try:
        with urlopen(req) as res:
            data = json.loads(res.read().decode())
            return data
    except HTTPError as e:
        if e.code == 401 and tries < 5:
            set_auth_token()
            return api_call(req, tries + 1)
        else:
            raise e


def venue_events():
    results = []
    stop = False
    page = 1
    while not stop:
        data = api_get(f'venues/{BACHELOR_VENUE_ID}/events', {'page': page})
        if not data or len(data) < 100:
            stop = True
        results = results + data
        page += 1

    return results


def get_all_quotes(event_id):
    params = {
        'q': f'event_id:{event_id}',
        'fields': 'quote::default,quote:shuttle_times,location::default,location:timezone,location:site_url,location:address2,location:description,location:msa,location:rating_summary',
        'option_types': 'all',
        'returns': 'curated offstreet_bookable_sold_out offstreet_bookable',
        'email': args.email,
        'routing_style': 'parkwhiz',
        'capabilities': 'capture_plate:always'
    }
    return api_get('quotes', params)


def same_day(first_date, second_date):
    return first_date.year == second_date.year and first_date.month == second_date.month and first_date.day == second_date.day


if not args.dates:
    desired_date = datetime.now()
    if desired_date.hour > 9:
        desired_date = desired_date + timedelta(days=1)
    desired_date_string = f'{desired_date.year}-{desired_date.month}-{desired_date.day}'
    desired_raw_dates = [desired_date_string]
else:
    desired_raw_dates = args.dates.split(sep=",")

my_bookings = api_get('bookings')
my_booked_event_ids = list(map(lambda x: x['event_id'], my_bookings))


def map_desired_dates(str):
    date = datetime.strptime(str, '%Y-%m-%d')
    for booking in my_bookings:
        booking_start_date = datetime.fromisoformat(booking['start_time'])
        if same_day(date, booking_start_date) and 'cancelled_at' not in booking:
            print(f'{date} is already booked')
            return None

    return date


desired_dates = list(filter(lambda x: x is not None, map(map_desired_dates, desired_raw_dates)))

if len(desired_dates) < 1:
    print('There are no dates to book for')
    exit()


def is_desired_event(event):
    start_time = datetime.fromisoformat(event['start_time'])
    for date in desired_dates:
        if same_day(date, start_time):
            return True
    return False


def book_parking(quote):
    payload = {
        'customer_email': args.email,
        'quote_id': quote['id'],
        'final_price': 0.0,
        'plate_number': args.plates
    }

    if args.dry_run:
        return True

    response = api_post('bookings', payload)

    return response


def book_first_quote(quotes):
    for quote in quotes:
        for option in quote['purchase_options']:
            success = book_parking(option)
            if success:
                print(f'Booked space {option}')
                return True
    return False


q = queue.Queue()

for event in list(filter(is_desired_event, venue_events())): q.put(event)


def worker():
    while not q.empty():
        event = q.get()
        event_date = event['start_time']
        print(f'Fetching quotes for {event_date}...')
        try:
            event_quotes = get_all_quotes(event['id'])['data']
        except Exception as e:
            print(e)
            event_quotes = []

        available_quotes = list(filter(lambda quote: len(quote['purchase_options']) > 0, event_quotes))

        if len(available_quotes) > 0:
            print(f'Found quotes for {event_date}')
            success = book_first_quote(available_quotes)
            if success:
                q.task_done()
                continue
            else:
                print(f'Could not book {event_date}')
        else:
            print(f'No spaces for {event_date}, sleeping for {args.interval} seconds...')

        time.sleep(args.interval)
        q.put(event)
        q.task_done()


threading.Thread(target=worker, daemon=True).start()
try:
    q.join()
except KeyboardInterrupt:
    print('Shutting down...')