import json
with open('signal.txt', 'r') as content_file:
	content = content_file.read()

print content
print json.loads(content)['start']

a = [{'orderID': 'e7e8951a-7423-92f3-380c-e0153d2b4682', 'clOrdID': 'mm_bitmex_aywhhfQqT1mz2cfYLVtDKg', 'clOrdLinkID': '', 'account': 472209, 'symbol': 'XBTUSD', 'side': 'Buy', 'simpleOrderQty': None, 'orderQty': 1, 'price': 5671.5, 'displayQty': None, 'stopPx': None, 'pegOffsetValue': None, 'pegPriceType': '', 'currency': 'USD', 'settlCurrency': 'XBt', 'ordType': 'Limit', 'timeInForce': 'GoodTillCancel', 'execInst': 'ParticipateDoNotInitiate', 'contingencyType': '', 'exDestination': 'XBME', 'ordStatus': 'New', 'triggered': '', 'workingIndicator': True, 'ordRejReason': '', 'simpleLeavesQty': None, 'leavesQty': 1, 'simpleCumQty': None, 'cumQty': 0, 'avgPx': None, 'multiLegReportingType': 'SingleSecurity', 'text': 'Submitted via API.', 'transactTime': '2019-05-05T08:04:07.480Z', 'timestamp': '2019-05-05T08:04:07.480Z'}]

print(a[0]['orderID'])