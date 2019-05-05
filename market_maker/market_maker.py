from __future__ import absolute_import
import time
from time import sleep
import sys
from datetime import datetime
from os.path import getmtime
import random
import requests
import atexit
import signal
import json

from market_maker import bitmex

from market_maker.settings import settings
from market_maker.utils import log, constants, errors, math


# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]


#
# Helpers
#
logger = log.setup_custom_logger('root')


class ExchangeInterface:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        if len(sys.argv) > 1:
            self.symbol = sys.argv[1]
        else:
            self.symbol = settings.SYMBOL
        self.bitmex = bitmex.BitMEX(base_url=settings.BASE_URL, symbol=self.symbol,
                                    apiKey=settings.API_KEY, apiSecret=settings.API_SECRET,
                                    orderIDPrefix=settings.ORDERID_PREFIX, postOnly=settings.POST_ONLY,
                                    timeout=settings.TIMEOUT)

    def cancel_order(self, order):
        tickLog = self.get_instrument()['tickLog']
        logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
        while True:
            try:
                self.bitmex.cancel(order['orderID'])
                sleep(settings.API_REST_INTERVAL)
            except ValueError as e:
                logger.info(e)
                sleep(settings.API_ERROR_INTERVAL)
            else:
                break

    def cancel_all_orders(self):
        if self.dry_run:
            return

        logger.info("Resetting current position. Canceling all existing orders.")
        tickLog = self.get_instrument()['tickLog']

        # In certain cases, a WS update might not make it through before we call this.
        # For that reason, we grab via HTTP to ensure we grab them all.
        orders = self.bitmex.http_open_orders()

        for order in orders:
            logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))

        if len(orders):
            self.bitmex.cancel([order['orderID'] for order in orders])

        sleep(settings.API_REST_INTERVAL)

    def get_portfolio(self):
        contracts = settings.CONTRACTS
        portfolio = {}
        for symbol in contracts:
            position = self.bitmex.position(symbol=symbol)
            instrument = self.bitmex.instrument(symbol=symbol)

            if instrument['isQuanto']:
                future_type = "Quanto"
            elif instrument['isInverse']:
                future_type = "Inverse"
            elif not instrument['isQuanto'] and not instrument['isInverse']:
                future_type = "Linear"
            else:
                raise NotImplementedError("Unknown future type; not quanto or inverse: %s" % instrument['symbol'])

            if instrument['underlyingToSettleMultiplier'] is None:
                multiplier = float(instrument['multiplier']) / float(instrument['quoteToSettleMultiplier'])
            else:
                multiplier = float(instrument['multiplier']) / float(instrument['underlyingToSettleMultiplier'])

            portfolio[symbol] = {
                "currentQty": float(position['currentQty']),
                "futureType": future_type,
                "multiplier": multiplier,
                "markPrice": float(instrument['markPrice']),
                "spot": float(instrument['indicativeSettlePrice'])
            }

        return portfolio

    def calc_delta(self):
        """Calculate currency delta for portfolio"""
        portfolio = self.get_portfolio()
        spot_delta = 0
        mark_delta = 0
        for symbol in portfolio:
            item = portfolio[symbol]
            if item['futureType'] == "Quanto":
                spot_delta += item['currentQty'] * item['multiplier'] * item['spot']
                mark_delta += item['currentQty'] * item['multiplier'] * item['markPrice']
            elif item['futureType'] == "Inverse":
                spot_delta += (item['multiplier'] / item['spot']) * item['currentQty']
                mark_delta += (item['multiplier'] / item['markPrice']) * item['currentQty']
            elif item['futureType'] == "Linear":
                spot_delta += item['multiplier'] * item['currentQty']
                mark_delta += item['multiplier'] * item['currentQty']
        basis_delta = mark_delta - spot_delta
        delta = {
            "spot": spot_delta,
            "mark_price": mark_delta,
            "basis": basis_delta
        }
        return delta

    def get_delta(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.get_position(symbol)['currentQty']

   #my custom function
    def get_all_data(self, symbol=None):
        return self.bitmex.get_all_data()

    def get_market_depth(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.market_depth(symbol)

    def get_instrument(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.instrument(symbol)

    def get_margin(self):
        if self.dry_run:
            return {'marginBalance': float(settings.DRY_BTC), 'availableFunds': float(settings.DRY_BTC)}
        return self.bitmex.funds()

    def get_orders(self):
        if self.dry_run:
            return []
        return self.bitmex.open_orders()

    def get_highest_buy(self):
        buys = [o for o in self.get_orders() if o['side'] == 'Buy']
        if not len(buys):
            return {'price': -2**32}
        highest_buy = max(buys or [], key=lambda o: o['price'])
        return highest_buy if highest_buy else {'price': -2**32}

    def get_lowest_sell(self):
        sells = [o for o in self.get_orders() if o['side'] == 'Sell']
        if not len(sells):
            return {'price': 2**32}
        lowest_sell = min(sells or [], key=lambda o: o['price'])
        return lowest_sell if lowest_sell else {'price': 2**32}  # ought to be enough for anyone

    def get_position(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.position(symbol)

    def get_ticker(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.ticker_data(symbol)

    def is_open(self):
        """Check that websockets are still open."""
        return not self.bitmex.ws.exited

    def check_market_open(self):
        instrument = self.get_instrument()
        if instrument["state"] != "Open" and instrument["state"] != "Closed":
            raise errors.MarketClosedError("The instrument %s is not open. State: %s" %
                                           (self.symbol, instrument["state"]))

    def check_if_orderbook_empty(self):
        """This function checks whether the order book is empty"""
        instrument = self.get_instrument()
        if instrument['midPrice'] is None:
            raise errors.MarketEmptyError("Orderbook is empty, cannot quote")

    def amend_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.amend_bulk_orders(orders)

    def create_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.create_bulk_orders(orders)

    def cancel_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.cancel([order['orderID'] for order in orders])


class OrderManager:
    def __init__(self):
        self.exchange = ExchangeInterface(settings.DRY_RUN)
        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        atexit.register(self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        logger.info("Using symbol %s." % self.exchange.symbol)

        if settings.DRY_RUN:
            logger.info("Initializing dry run. Orders printed below represent what would be posted to BitMEX.")
        else:
            logger.info("Order Manager initializing, connecting to BitMEX. Live run: executing real trades.")

        self.start_time = datetime.now()
        # logger.info("0-0-0-0-0-0-0-0-0-0--------Starting--------0-0-0-0-0-0-0-0-")
        self.instrument = self.exchange.get_instrument()
        print ("\n\n",self.instrument)
        logger.info("0-0-0-0-0-0-0-0-0-0--------Instrument printed Successfully--------0-0-0-0-0-0-0-0-")
        self.starting_qty = self.exchange.get_delta()
        print("\n\n\n", self.starting_qty )
        logger.info("0-0-0-0-0-0-0-0-0-0--------starting quantity printed Successfully--------0-0-0-0-0-0-0-0-")
        self.running_qty = self.starting_qty
        print("\n\n\n", self.running_qty)
        logger.info("0-0-0-0-0-0-0-0-0-0--------running quantity printed Successfully--------0-0-0-0-0-0-0-0-")
        self.reset()

    def reset(self):
        self.exchange.cancel_all_orders()
        logger.info("\n\n\n0-0-0-0-0-0-0-0-0-0------- All ordres cancelled --------0-0-0-0-0-0-0-0-")
        self.sanity_check()
        logger.info("\n\n\n0-0-0-0-0-0-0-0-0-0------- Sanity Check is Done --------0-0-0-0-0-0-0-0-")
        self.print_status()
        logger.info("\n\n\n0-0-0-0-0-0-0-0-0-0------- Status Printed Successfully --------0-0-0-0-0-0-0-0-")
        # Create orders and converge.
        self.place_orders()
        logger.info("\n\n\n0-0-0-0-0-0-0-0-0-0------- Orders Placed --------0-0-0-0-0-0-0-0-")

    def print_status(self):
        """Print the current MM status."""

        margin = self.exchange.get_margin()
        position = self.exchange.get_position()
        self.running_qty = self.exchange.get_delta()
        tickLog = self.exchange.get_instrument()['tickLog']
        self.start_XBt = margin["marginBalance"]

        logger.info("Current XBT Balance: %.6f" % XBt_to_XBT(self.start_XBt))
        logger.info("Current Contract Position: %d" % self.running_qty)
        if settings.CHECK_POSITION_LIMITS:
            logger.info("Position limits: %d/%d" % (settings.MIN_POSITION, settings.MAX_POSITION))
        if position['currentQty'] != 0:
            logger.info("Avg Cost Price: %.*f" % (tickLog, float(position['avgCostPrice'])))
            logger.info("Avg Entry Price: %.*f" % (tickLog, float(position['avgEntryPrice'])))
        logger.info("Contracts Traded This Run: %d" % (self.running_qty - self.starting_qty))
        logger.info("Total Contract Delta: %.4f XBT" % self.exchange.calc_delta()['spot'])

    def get_ticker(self):
        ticker = self.exchange.get_ticker()
        tickLog = self.exchange.get_instrument()['tickLog']

        # Set up our buy & sell positions as the smallest possible unit above and below the current spread
        # and we'll work out from there. That way we always have the best price but we don't kill wide
        # and potentially profitable spreads.
        self.start_position_buy = ticker["buy"] + self.instrument['tickSize']
        self.start_position_sell = ticker["sell"] - self.instrument['tickSize']

        # If we're maintaining spreads and we already have orders in place,
        # make sure they're not ours. If they are, we need to adjust, otherwise we'll
        # just work the orders inward until they collide.
        if settings.MAINTAIN_SPREADS:
            if ticker['buy'] == self.exchange.get_highest_buy()['price']:
                self.start_position_buy = ticker["buy"]
            if ticker['sell'] == self.exchange.get_lowest_sell()['price']:
                self.start_position_sell = ticker["sell"]

        # Back off if our spread is too small.
        if self.start_position_buy * (1.00 + settings.MIN_SPREAD) > self.start_position_sell:
            self.start_position_buy *= (1.00 - (settings.MIN_SPREAD / 2))
            self.start_position_sell *= (1.00 + (settings.MIN_SPREAD / 2))

        # Midpoint, used for simpler order placement.
        self.start_position_mid = ticker["mid"]
        logger.info(
            "%s Ticker: Buy: %.*f, Sell: %.*f" %
            (self.instrument['symbol'], tickLog, ticker["buy"], tickLog, ticker["sell"])
        )
        logger.info('Start Positions: Buy: %.*f, Sell: %.*f, Mid: %.*f' %
                    (tickLog, self.start_position_buy, tickLog, self.start_position_sell,
                     tickLog, self.start_position_mid))
        return ticker

    def get_price_offset(self, index):
        """Given an index (1, -1, 2, -2, etc.) return the price for that side of the book.
           Negative is a buy, positive is a sell."""
        # Maintain existing spreads for max profit
        if settings.MAINTAIN_SPREADS:
            start_position = self.start_position_buy if index < 0 else self.start_position_sell
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        else:
            # Offset mode: ticker comes from a reference exchange and we define an offset.
            start_position = self.start_position_buy if index < 0 else self.start_position_sell

            # If we're attempting to sell, but our sell price is actually lower than the buy,
            # move over to the sell side.
            if index > 0 and start_position < self.start_position_buy:
                start_position = self.start_position_sell
            # Same for buys.
            if index < 0 and start_position > self.start_position_sell:
                start_position = self.start_position_buy

        return math.toNearest(start_position * (1 + settings.INTERVAL) ** index, self.instrument['tickSize'])

# =-=-=--=-=-=-=-=-=-=-=-=-custom functions start==p-=-=-=-=-=-=-=-=--=
    def get_price_history(self, period=None, start_time=None, end_time=None):
        test_net_url = 'https://www.bitmex.com/api/udf/history?symbol=XBTUSD&resolution=5&from=1556882941&to=1556969401'
        if period is None:
            period = 1

        if end_time is None:
            end_time = int(time.time())
        if start_time is None:
            start_time = end_time - 84000
        # for testnet
        # url = 'https://testnet.bitmex.com/api/udf/history?symbol=XBTUSD&resolution=' + str(period) + '&from=' + str(start_time) + '&to=' + str(end_time)
        #uncomment if for real trading
        url = 'https://www.bitmex.com/api/udf/history?symbol=XBTUSD&resolution='+ str(period) + '&from=' + str(start_time) + '&to=' + str(end_time)
        def exit_or_throw(e):
            logger.warning("-=-=-=--=-=-=-=rethrow errorss")
            # if rethrow_errors:
            #     raise e
            # else:
            #     exit(1)

        def retry():
            self.retries += 1
            if self.retries > max_retries:
                raise Exception("Max retries on %s (%s) hit, raising." % (path, json.dumps(postdict or '')))
            return self.get_price_history()

        # Make the request
        response = None
        try:
            logger.info("sending req to %s" % (url))
            response = requests.get(url)
            # prepped = self.session.prepare_request(req)
            # response = self.session.send(prepped, timeout=timeout)
            # Make non-200s throw
            response.raise_for_status()

        except requests.exceptions.HTTPError as e:
            if response is None:
                raise e

            # 401 - Auth error. This is fatal.
            if response.status_code == 401:
                logger.error("API Key or Secret incorrect, please check and restart.")
                logger.error("Error: " + response.text)
                if postdict:
                    logger.error(postdict)
                # Always exit, even if rethrow_errors, because this is fatal
                exit(1)

            # 404, can be thrown if order canceled or does not exist.
            elif response.status_code == 404:
                if verb == 'DELETE':
                    logger.error("Order not found: %s" % postdict['orderID'])
                    return
                logger.error("Unable to contact the BitMEX API (404). " +
                                  "Request: %s \n %s" % (url, json.dumps(postdict)))
                exit_or_throw(e)

            # 429, ratelimit; cancel orders & wait until X-RateLimit-Reset
            elif response.status_code == 429:
                logger.error("Request Failed with the Error code 429")
                # logger.error("Ratelimited on current request. Sleeping, then trying again. Try fewer " +
                #                   "order pairs or contact support@bitmex.com to raise your limits. " +
                #                   "Request: %s \n %s" % (url, json.dumps(postdict)))

                # # Figure out how long we need to wait.
                # ratelimit_reset = response.headers['X-RateLimit-Reset']
                # to_sleep = int(ratelimit_reset) - int(time.time())
                # reset_str = datetime.datetime.fromtimestamp(int(ratelimit_reset)).strftime('%X')

                # # We're ratelimited, and we may be waiting for a long time. Cancel orders.
                # logger.warning("Canceling all known orders in the meantime.")
                # cancel([o['orderID'] for o in self.open_orders()])

                # logger.error("Your ratelimit will reset at %s. Sleeping for %d seconds." % (reset_str, to_sleep))
                # time.sleep(to_sleep)

                # # Retry the request.
                # return retry()

            # 503 - BitMEX temporary downtime, likely due to a deploy. Try again
            elif response.status_code == 503:
                # logger.warning("Unable to contact the BitMEX API (503), retrying. " +
                #                     "Request: %s \n %s" % (url, json.dumps(postdict)))
                # time.sleep(3)
                logger.error("Request Failed with the Error code 503, retrying")
                return retry()

            elif response.status_code == 400:
                logger.error("Request Failed with the Error code 400")
                # error = response.json()['error']
                # message = error['message'].lower() if error else ''

                # # Duplicate clOrdID: that's fine, probably a deploy, go get the order(s) and return it
                # if 'duplicate clordid' in message:
                #     orders = postdict['orders'] if 'orders' in postdict else postdict

                #     IDs = json.dumps({'clOrdID': [order['clOrdID'] for order in orders]})
                #     orderResults = self._curl_bitmex('/order', query={'filter': IDs}, verb='GET')

                #     for i, order in enumerate(orderResults):
                #         if (
                #                 order['orderQty'] != abs(postdict['orderQty']) or
                #                 order['side'] != ('Buy' if postdict['orderQty'] > 0 else 'Sell') or
                #                 order['price'] != postdict['price'] or
                #                 order['symbol'] != postdict['symbol']):
                #             raise Exception('Attempted to recover from duplicate clOrdID, but order returned from API ' +
                #                             'did not match POST.\nPOST data: %s\nReturned order: %s' % (
                #                                 json.dumps(orders[i]), json.dumps(order)))
                #     # All good
                #     return orderResults

                # elif 'insufficient available balance' in message:
                #     logger.error('Account out of funds. The message: %s' % error['message'])
                #     exit_or_throw(Exception('Insufficient Funds'))


            # If we haven't returned or re-raised yet, we get here.
            logger.error("Unhandled Error: %s: %s" % (e, response.text))
            exit_or_throw(e)

        except requests.exceptions.Timeout as e:
            # Timeout, re-run this request
            logger.warning("Timed out on request: %s, retrying..." % (path))
            return retry()

        except requests.exceptions.ConnectionError as e:
            logger.warning("Unable to contact the BitMEX API (%s). Please check the URL. Retrying. " +
                                "Request: %s %s \n" % (e, url))
            time.sleep(1)
            return retry()

        # Reset retry counter on success
        self.retries = 0

        return response.json()

    #-=-=-=-=-=-=-=-=-=custom functions end-----0-=-=-=-=-=--=

    ###
    # Orders
    ###

    previous_buy_orders = []
    previous_sell_orders = []
    buy_orders = []
    sell_orders = []
    loop_count = 0
    def place_orders(self):
        """Create order items for use in convergence."""
        print("Place Orde Loop Count=",self.loop_count)
        self.loop_count += 1
        self.buy_orders = []
        self.sell_orders = []
        ''' # commenting for testing
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        for i in reversed(range(1, settings.ORDER_PAIRS + 1)):
            if not self.long_position_limit_exceeded():
                buy_orders.append(self.prepare_order(-i))
            if not self.short_position_limit_exceeded():
                sell_orders.append(self.prepare_order(i))
        '''
        # print('-=-=-=-==-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-TEST BENCH-==-=-=-=----=-=-=-=-=-=-=--=-=-=-=-=-=-=-=-=-=-=-=-=-=-=')
        # print("\n\n=-=-=-= print  get_instrument")
        # print(self.exchange.get_instrument())
        # print("\n\n=-=-=-= print  get_all_data")
        # print(self.exchange.get_all_data())
        # print("\n\n=-=-=-= print  get_portfolio")
        # print(self.exchange.get_portfolio())
        # print("\n\n=-=-=-= print  calc_delta")
        # print(self.exchange.calc_delta())
        # print("\n\n=-=-=-= print  get_delta")
        # print(self.exchange.get_delta())
        # print("\n\n=-=-=-= print  get_instrument")
        # print(self.exchange.get_instrument())
        # print("\n\n=-=-=-= print  get_margin")
        # print(self.exchange.get_margin())
        # print("\n\n=-=-=-= print  get_orders")
        # print(self.exchange.get_orders())
        # print("\n\n=-=-=-= print  get_highest_buy")
        # print(self.exchange.get_highest_buy())
        # print("\n\n=-=-=-= print   get_lowest_sell")
        # print(self.exchange.get_lowest_sell())
        # print("\n\n=-=-=-= print  get_position")
        # print(self.exchange.get_position())
        # print("\n\n=-=-=-= print get_ticker")
        # print(self.exchange.get_ticker())
        # print("\n\n=-=-=-= print  is_open")
        # print(self.exchange.is_open())
        # print("\n\n=-=-=-= print  check_market_open")
        # print(self.exchange.check_market_open())
        # print("\n\n=-=-=-= print  check_if_orderbook_empty")
        # print(self.exchange.check_if_orderbook_empty())
        # print("\n\n=-=-=-= print  print historyical price")
        # print(self.get_price_history())
        # print('-=-=-=-==-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=--=-=-=-=-=-=-TEST BENCH SLEEPING-==-=-=-=-----=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=')
        # # sleep(1000)

        with open('/home/ec2-user/signal.txt', 'r') as content_file:
            content = content_file.read()

        signal_content = json.loads(content)
        print ("Signal File contenet is ",signal_content)
        #buy if the singal is 1

        order_quantity = 1
        ticker = self.exchange.get_ticker()
        print("-=-=-=-=-=-=-=ticker is =",ticker,"-=-=-=-=-=-=-=")
        bid_price = ticker['buy']
        ask_price = ticker['sell']
        mid_price = ticker['mid']
        position = self.exchange.get_position()
        print("-=-=-=-=-=-=-=open positions is = current qty",position['currentQty'], " avg entyr price=", position["avgEntryPrice"], "-=-=-=-=-=-=-=")
        open_orders = self.exchange.get_orders()
        running_quantity = self.exchange.get_delta()
        try:
            
            print("-=-=-=-=-=-=-=open order is =\norder quantity=",open_orders[0]['orderQty'],"\norder price=", open_orders[0]['price'], "-=-=-=-=-=-=-=")
        except:
            None


        """
        To bots running in two accounts and one plces buy order and another place sell order at the same time.
        long_order_bot() is runs on one account aand it palces long order first , 
        short_order-bot() runs in another account and places short orders.
        singal.txt file is used to control the bot, value 1 means long in first bot and short in second bot, and vice versa for value 2
        """



        def long_order_bot():
            logger.info("this is SHORT order Bot")
            # start when the signal.txt file has 1
            if signal_content['start'] == 1:
                print ("=-=-=-=singla buying now -=-=-=-=-=-=-=")
                #no existing contacts and no existing orders then place one order
                if self.exchange.get_delta() < 1 and not open_orders:
                    self.buy_orders.append({'price': bid_price, 'orderQty': 1, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                # exisitng order is not one the top the order book. ammend the order
                elif open_orders and float(open_orders[0]['price']) < bid_price:
                    print("Amending a the order")
                    self.buy_orders.append({'price': bid_price, 'orderQty': 1, 'side': "Buy"})
                    self.previous_buy_orders = self.uy_orders
                # order exists and on top of the order of the orderbook. return the same order. 
                elif open_orders:
                    self.buy_orders = self.previous_buy_orders
                # sell_orders.append({'price': 5800.0, 'orderQty': 111, 'side': "Sell"})

            elif signal_content['start'] == 2:
                print('=-=-=-=-=-=-=selling now =-=-=-')

                print("-=-=-=running quantity= ",running_quantity)
                # if open_orders and float(position['avgEntryPrice']) > ask_price:
                if running_quantity > 0 and not open_orders:
                    print("s=-=-=-=-= quantity exisit and no open orders ")
                    self.sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders and float(open_orders[0]['price']) > ask_price:
                    print("0000000000 =-=-=-=-=-=-=-==-=-=-open order with greater than ask price.")
                    self.sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders:
                    self.sell_orders = self.previous_sell_orders

            print("=0=-0-0-0-going to return teh order limits")

        def short_order_bot():
            logger.info("this is SHORT order Bot")
            if signal_content['start'] == 1:
                print ("=-=-=-=singla selling now -=-=-=-=-=-=-=")
                print ("delat avalue is ",self.exchange.get_delta())
                if self.exchange.get_delta() >= 0 and not open_orders:
                    print("no short existing and no open orders, creating new orde")
                    sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders and float(open_orders[0]['price']) > ask_price:
                    print("Amending a the order")
                    sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders:
                    self.sell_orders = self.previous_sell_orders

                # sell_orders.append({'price': 5800.0, 'orderQty': 111, 'side': "Sell"})

            elif signal_content['start'] == 2:
                print('=-=-=-=-=-=-=selling now =-=-=-')

                print("-=-=-=running quantity= ",running_quantity)
                # if open_orders and float(position['avgEntryPrice']) > ask_price:
                if running_quantity > 0 and not open_orders:
                    print("s=-=-=-=-= quantity exisit and no open orders ")
                    sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})
                elif open_orders and float(open_orders[0]['price']) > ask_price:
                    print("0000000000 =-=-=-=-=-=-=-==-=-=-open order with greater than ask price.")
                    sell_orders.append({'price': ask_price, 'orderQty': 1, 'side': "Sell"})

            print("=0=-0-0-0-going to return teh order limits")
        
        long_order_bot()
        return self.converge_orders(self.buy_orders, self.sell_orders)

    def prepare_order(self, index):
        """Create an order object."""

        if settings.RANDOM_ORDER_SIZE is True:
            quantity = random.randint(settings.MIN_ORDER_SIZE, settings.MAX_ORDER_SIZE)
        else:
            quantity = settings.ORDER_START_SIZE + ((abs(index) - 1) * settings.ORDER_STEP_SIZE)

        price = self.get_price_offset(index)

        return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def converge_orders(self, buy_orders, sell_orders):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            try:
                if order['side'] == 'Buy':
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                else:
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)

        while buys_matched < len(buy_orders):
            to_create.append(buy_orders[buys_matched])
            buys_matched += 1

        while sells_matched < len(sell_orders):
            to_create.append(sell_orders[sells_matched])
            sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_bulk_orders(to_cancel)

    ###
    # Position Limits
    ###

    def short_position_limit_exceeded(self):
        """Returns True if the short position limit is exceeded"""
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position <= settings.MIN_POSITION

    def long_position_limit_exceeded(self):
        """Returns True if the long position limit is exceeded"""
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position >= settings.MAX_POSITION

    ###
    # Sanity
    ##

    def sanity_check(self):
        """Perform checks before placing orders."""

        # Check if OB is empty - if so, can't quote.
        self.exchange.check_if_orderbook_empty()

        # Ensure market is still open.
        self.exchange.check_market_open()

        # Get ticker, which sets price offsets and prints some debugging info.
        ticker = self.get_ticker()

        # Sanity check:
        if self.get_price_offset(-1) >= ticker["sell"] or self.get_price_offset(1) <= ticker["buy"]:
            logger.error("Buy: %s, Sell: %s" % (self.start_position_buy, self.start_position_sell))
            logger.error("First buy position: %s\nBitMEX Best Ask: %s\nFirst sell position: %s\nBitMEX Best Bid: %s" %
                         (self.get_price_offset(-1), ticker["sell"], self.get_price_offset(1), ticker["buy"]))
            logger.error("Sanity check failed, exchange data is inconsistent")
            self.exit()

        # Messaging if the position limits are reached
        if self.long_position_limit_exceeded():
            logger.info("Long delta limit exceeded")
            logger.info("Current Position: %.f, Maximum Position: %.f" %
                        (self.exchange.get_delta(), settings.MAX_POSITION))

        if self.short_position_limit_exceeded():
            logger.info("Short delta limit exceeded")
            logger.info("Current Position: %.f, Minimum Position: %.f" %
                        (self.exchange.get_delta(), settings.MIN_POSITION))

    ###
    # Running
    ###

    def check_file_change(self):
        """Restart if any files we're watching have changed."""
        for f, mtime in watched_files_mtimes:
            if getmtime(f) > mtime:
                self.restart()

    def check_connection(self):
        """Ensure the WS connections are still open."""
        return self.exchange.is_open()

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

        sys.exit()

    def run_loop(self):
        while True:
            sys.stdout.write("-----\n")
            sys.stdout.flush()

            self.check_file_change()
            sleep(settings.LOOP_INTERVAL)

            # This will restart on very short downtime, but if it's longer,
            # the MM will crash entirely as it is unable to connect to the WS on boot.
            if not self.check_connection():
                logger.error("Realtime data connection unexpectedly closed, restarting.")
                self.restart()

            self.sanity_check()  # Ensures health of mm - several cut-out points here
            self.print_status()  # Print skew, delta, etc
            self.place_orders()  # Creates desired orders and converges to existing orders

    def restart(self):
        logger.info("Restarting the market maker...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

#
# Helpers
#


def XBt_to_XBT(XBt):
    return float(XBt) / constants.XBt_TO_XBT


def cost(instrument, quantity, price):
    mult = instrument["multiplier"]
    P = mult * price if mult >= 0 else mult / price
    return abs(quantity * P)


def margin(instrument, quantity, price):
    return cost(instrument, quantity, price) * instrument["initMargin"]


def run():
    logger.info('BitMEX Market Maker Version: %s\n' % constants.VERSION)

    om = OrderManager()
    # order_manager = CustomOrderManager()
    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        om.run_loop()
        # order_manager.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
