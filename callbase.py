import requests
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

uuid_list = []

@app.route('/')
def index():
    print("Root route accessed")
    return "Flask server is running!"

@app.route('/webhook/<chatid>/<scriptid>', methods=['POST'])
def webhook(chatid, scriptid):
    print(f"Webhook accessed with chatid: {chatid}, scriptid: {scriptid}")
    data = request.json  
    event = data.get('state')
    
    print(f"Received event: {event}")
    
    if event == 'call.answered':
        if uuid_list:
            url = ""
            payload = {
                'uuid': uuid_list[0],
                'audiourl': '',
                'maxdigits': '1'
            }
            try:
                response = requests.post(url=url, json=payload)
                response.raise_for_status()
                print(f"Audio gather response: {response.text}")
            except requests.RequestException as e:
                print(f"Failed to gather audio: {e}")
    
    elif event == 'dtmf.gathered':
        digits = data.get('digits')
        print(f"Gathered digits: {digits}")

    elif event == 'dtmf.entered':
        digit = data.get('digit')
        print(f"Entered digit: {digit}")

    response_data = {'status': 'success'}
    return jsonify(response_data), 200

@app.route('/call', methods=['GET'])
def makecall():
    print("Call route accessed")
    url = ""
    
    payload = {
        'api_key': 'daadd383-3eb8-4554-84d1-7ec12eebda9d',
        'callbackURL': '',
        'to_': "919657557562",
        'from_': '917076523944'
    }
    try:
        r = requests.post(url, json=payload)
        r.raise_for_status()  # Raises an exception for 4XX/5XX responses
        res = r.json()
        uuid_list.append(res.get('uuid', ''))
        print(f"Call initiated, UUID: {res.get('uuid', 'Unknown')}")
    except requests.RequestException as e:
        print(f"Failed to make call: {e}")
        return jsonify({'status': 'failure'}), 500

    response_data = {'status': 'success'}
    return jsonify(response_data), 200

if __name__ == '__main__':
    app.run(port=5000, debug=True)
