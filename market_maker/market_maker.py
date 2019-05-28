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
import numpy
import talib
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
        if period is None:
            period = 1

        if end_time is None:
            end_time = int(time.time())
        if start_time is None:
            # 48000 if period is 10 , 800 data points
            start_time = end_time - 259200
             #777600
            # one hour graph time with 30 days period
            # start_time = end_time - 2592000
        # for testnet
        # print("Start Time=", start_time, "Endtime = ", end_time)
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
            print("Retrying the request again . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . ")
            # self.retries += 1
            # if self.retries > max_retries:
                # raise Exception("Max retries on %s (%s) hit, raising." % (path, json.dumps(postdict or '')))
            return self.get_price_history()

        # Make the request
        response = None
        try:
            # logger.info("sending req to %s" % (url))
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

            elif response.status_code !=200:
                return retry()

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

            # 503 - BitMEX temporary downtime, likely due to a deploy. Try again
            elif response.status_code == 503:
                # logger.warning("Unable to contact the BitMEX API (503), retrying. " +
                #                     "Request: %s \n %s" % (url, json.dumps(postdict)))
                # time.sleep(3)
                logger.error("Request Failed with the Error code 503, retrying")
                return retry()

            elif response.status_code == 400:
                logger.error("Request Failed with the Error code 400")

            elif response.status.code !=200:
                logger.error("Request failed with non 200 error code, retyring")

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

    def get_current_macd_value(self):
        # logger.info("Calculating the current MACD Value")
        price_history = self.get_price_history()
        # print(price_history)
        close_price_array = numpy.array(price_history['c'])
        # macd, macdsignal, macdhist = talib.MACD(close_price_array, fastperiod=12, slowperiod=26, signalperiod=9)
        macd, macdsignal, macdhist = talib.MACD(close_price_array, fastperiod=12, slowperiod=26, signalperiod=9)

        sma = talib.SMA(macd, timeperiod=8)

        length = len(macd)
        print("leng=",length)

        # print("=-=-=-=-=-=-=-=-=-=-=printitng EMA")
        # print (sma)
        # print("=-=-=-=-=-=-=-=-=-=-=printitng EMA")
        # -2 gives the previous macd value, latest macd value can fluctuate with price.
        macd_values = {
            "macd": macd[length-2],
            "macdsignal": macdsignal[length-2],
            "macdhist": macdhist[length-2]
        }

        for i in range(2):
            i += 1
            print("Latest Price: epoc time", price_history['t'][length-i], "Time=", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(price_history['t'][length-i])), "Open=" ,price_history['o'][length-i], "High=", price_history['h'][length-i], "Low=", price_history['l'][length-i], "Close=", price_history['c'][length-i])
            print("Current MACD = ", price_history['t'][length-i], " Macd=",macd[length-i]," macdsignal=",macdsignal[length-i]," macdsignal_SMA=",sma[length-i]," macdhist=",macdhist[length-i], "\n")

        return macd_values
    #-=-=-=-=-=-=-=-=-=custom functions end-----0-=-=-=-=-=--=

    ###
    # Orders
    ###

    macd_delay_counter = 0

    previous_buy_orders = []
    previous_sell_orders = []
    buy_orders = []
    sell_orders = []
    loop_count = 0

    macd_up_flag = 0
    macd_down_flag = 0

    order_quantity = 1
    # when trade complete, both buy and sell is done,
    buy_trade_completed = 0
    sell_trade_completed = 0

    buy_order_pending = 0
    sell_order_pending = 0

    sell_order_placed = 0
    buy_order_placed = 0

    order_signal_status = -1

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

        # with open('/home/ec2-user/signal.txt', 'r') as content_file:
        #     content = content_file.read()

        # signal_content = json.loads(content)
        # print ("Signal File contenet is ",signal_content)
        #buy if the singal is 1

        ticker = self.exchange.get_ticker()
        print("-=-=-=-=-=-=-=ticker is =",ticker,"-=-=-=-=-=-=-=")
        bid_price = ticker['buy']
        ask_price = ticker['sell']
        mid_price = ticker['mid']
        position = self.exchange.get_position()
        print("Printing current Order book ifno bid_price, ask_price, mid_price= ",bid_price,ask_price,mid_price)
        # print("dumps current postion info", position)
        print("-=-=-=-=-=-=-=open positions is = current qty",position['currentQty'], " avg entyr price=", position["avgEntryPrice"], "-=-=-=-=-=-=-=")
        open_orders = self.exchange.get_orders()
        running_quantity = self.exchange.get_delta()
        try:
            print("-=-=-=-=-=-=-=open order is =\norder quantity=",open_orders[0]['orderQty'],"\norder price=", open_orders[0]['price'], "-=-=-=-=-=-=-=")
        except:
            None

        # delta = self.exchange.get_delta()
        """
        To bots running in two accounts and one plces buy order and another place sell order at the same time.
        long_order_bot() is runs on one account aand it palces long order first , 
        short_order-bot() runs in another account and places short orders.
        singal.txt file is used to control the bot, value 1 means long in first bot and short in second bot, and vice versa for value 2
        """


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
        """


        def get_order_singal():
            macd_values = self.get_current_macd_value()
            # print("MACD Values from functions = ", macd_values)
            if self.macd_up_flag == 0 and self.macd_down_flag ==0:
                if macd_values['macd'] > macd_values['macdsignal']:
                    self.macd_up_flag = 1
                else:
                    self.macd_down_flag = 1


            if macd_values['macd'] > macd_values['macdsignal'] and self.macd_down_flag:
                print ("MACD Crossed - UPTREND")
                self.macd_up_flag = 1
                self.macd_down_flag = 0
                return 1

            elif macd_values['macd'] < macd_values['macdsignal'] and self.macd_up_flag:
                print ("MACD Crossed - DownTrend")
                self.macd_down_flag = 1
                self.macd_up_flag = 0
                return -1

            if self.macd_up_flag:
                print("Currently in uptreand. No action required")
            if self.macd_down_flag:
                print("Currently in DownTrend. No action required")
            return 0
                

        def place_buy_order():
            print("Inside Long Order Functions")
            # to check when the trade is complete, if the trade is complete then no more trade is done.
            # need to change the way trade completeion is handled, use a flag when the loop goes into the sell section.
            print("Running Qantity=",running_quantity," open_orders=",open_orders," order_signal_status=",self.order_signal_status, "buy_order_pending=",self.buy_order_pending, " buy_trade_completed=",self.buy_trade_completed)
            if running_quantity < 1 and not open_orders and self.sell_order_placed == 1:
                # self.buy_trade_completed = 1
                self.sell_order_placed = 0
                self.buy_order_pending = 0
                print("---Inside order completed loop")
                return

            # if not self.buy_trade_completed:
            print("inside order not completed Section")
            if running_quantity < 1:
                print("inside buy loop")
                if not open_orders:
                    print("Insde first buy order section")
                    self.buy_orders.append({'price': bid_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                elif open_orders and float(open_orders[0]['price']) < bid_price:
                    print("Inside Amending a the order ot price= ",bid_price)
                    self.buy_orders.append({'price': bid_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                    print("previous_buy_orders variabel has value = ",self.previous_buy_orders)
                elif open_orders:
                    print("Inside No action required section")
                    self.buy_orders = self.previous_buy_orders
            elif running_quantity >= 1:
                print("inside sell loop")
                self.sell_order_placed = 1
                # if the sell is completed withing the next loop then position["avgEntryPrice"] has null value which throws exception
                try:
                    pl_delta = ask_price - position["avgEntryPrice"]
                except Exception as e:
                    return None

                print ("Delta Price = ",pl_delta)
                if not open_orders and position["avgEntryPrice"] <= ask_price:
                    print("placing the initial sell order")
                    self.sell_orders.append({'price': ask_price, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders and float(open_orders[0]['price']) > ask_price and position["avgEntryPrice"] <= ask_price:
                    print("Ammend the sell order. open order with greater than ask price.")
                    self.sell_orders.append({'price': ask_price, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif pl_delta <= -10 or self.macd_down_flag == 1:
                    print("inside stoploss loop")
                    self.sell_orders.append({'price': ask_price, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders:
                    print(" no action to sell order")
                    self.sell_orders = self.previous_sell_orders

# stratey used +5 for profit and macd crossover for stoploss
        def place_buy_order_strat_1():
            print("Inside Long Order Functions")
            # to check when the trade is complete, if the trade is complete then no more trade is done.
            # need to change the way trade completeion is handled, use a flag when the loop goes into the sell section.
            print("Running Qantity=",running_quantity," open_orders=",open_orders," order_signal_status=",self.order_signal_status, "buy_order_pending=",self.buy_order_pending, " buy_trade_completed=",self.buy_trade_completed)
            if running_quantity < 1 and not open_orders and self.sell_order_placed == 1:
                # self.buy_trade_completed = 1
                self.sell_order_placed = 0
                self.buy_order_pending = 0
                print("---Inside order completed loop")
                return

            # if not self.buy_trade_completed:
            print("inside order not completed Section")
            if running_quantity < 1:
                print("inside buy loop")
                if not open_orders:
                    print("Insde first buy order section")
                    self.buy_orders.append({'price': bid_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                elif open_orders and float(open_orders[0]['price']) < bid_price:
                    print("Inside Amending a the order ot price= ",bid_price)
                    self.buy_orders.append({'price': bid_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                    print("previous_buy_orders variabel has value = ",self.previous_buy_orders)
                elif open_orders:
                    print("Inside No action required section")
                    self.buy_orders = self.previous_buy_orders
            elif running_quantity >= 1:
                print("inside sell loop")
                self.sell_order_placed = 1
                # if the sell is completed withing the next loop then position["avgEntryPrice"] has null value which throws exception
                try:
                    pl_delta = ask_price - position["avgEntryPrice"]
                except Exception as e:
                    return None

                print ("Delta Price = ",pl_delta)
                if not open_orders:
                    print("placing the initial sell order")
                    self.sell_orders.append({'price': position["avgEntryPrice"]+10, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif pl_delta <= -30 or self.macd_down_flag == 1:
                    print("inside stoploss loop")
                    self.sell_orders.append({'price': ask_price, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders:
                    print(" no action to sell order")
                    self.sell_orders = self.previous_sell_orders


# macd strategy uses market orders.
        def place_buy_order_market_orders_strat():
            running_quantity = self.exchange.get_delta()
            http_open_orders = self.bitmex.http_open_orders()
            print("Inside Long Order Functions")
            # to check when the trade is complete, if the trade is complete then no more trade is done.
            # need to change the way trade completeion is handled, use a flag when the loop goes into the sell section.
            print("Running Qantity=",running_quantity," open_orders=",http_open_orders," order_signal_status=",self.order_signal_status, "buy_order_pending=",self.buy_order_pending, " buy_trade_completed=",self.buy_trade_completed)
            if running_quantity < 1 and not http_open_orders and self.sell_order_placed == 1:
                # self.buy_trade_completed = 1
                self.sell_order_placed = 0
                self.buy_order_pending = 0
                print("---Inside order completed loop")
                return

            # if not self.buy_trade_completed:
            print("inside order not completed Section")
            if running_quantity < 1 :
                print("inside buy loop")
                http_open_orders = self.bitmex.http_open_orders()
                print("prining HTTP Open orders===",http_open_orders)

                if not http_open_orders:
                    print("Insde first buy order section")
                    self.buy_orders.append({'price': ask_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                    self.previous_buy_orders = self.buy_orders
                # elif open_orders and float(open_orders[0]['price']) < bid_price:
                #     print("Inside Amending a the order ot price= ",bid_price)
                #     self.buy_orders.append({'price': ask_price, 'orderQty': self.order_quantity, 'side': "Buy"})
                #     self.previous_buy_orders = self.buy_orders
                #     print("previous_buy_orders variabel has value = ",self.previous_buy_orders)

                # open order present but and no buy is done. 
                elif http_open_orders and self.exchange.get_delta() < 1:
                    print("Inside No action required section")
                    self.buy_orders = self.previous_buy_orders


            elif running_quantity >= 1:
                print("inside sell loop")
                self.sell_order_placed = 1
                # if the sell is completed withing the next loop then position["avgEntryPrice"] has null value which throws exception
                try:
                    pl_delta = ask_price - int(position["avgEntryPrice"])
                except Exception as e:
                    return None

                print ("Delta Price = ",pl_delta)
                if not open_orders:
                    print("placing the initial sell order")
                    self.sell_orders.append({'price': int(position["avgEntryPrice"])+5, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                # elif open_orders and float(open_orders[0]['price']) > ask_price and position["avgEntryPrice"] <= ask_price:
                #     print("Ammend the sell order. open order with greater than ask price.")
                #     self.sell_orders.append({'price': bid_price, 'orderQty': running_quantity, 'side': "Sell"})
                #     self.previous_sell_orders = self.sell_orders
                elif self.macd_down_flag == 1:
                    print("inside stoploss loop")
                    self.sell_orders.append({'price': bid_price, 'orderQty': running_quantity, 'side': "Sell"})
                    self.previous_sell_orders = self.sell_orders
                elif open_orders:
                    print(" no action to sell order")
                    self.sell_orders = self.previous_sell_orders



        def place_sell_order():
            print("Placing Sell order ----")


        def macd_based_order_generator():
            self.macd_delay_counter += 1
            if self.macd_delay_counter >= 15:
                self.order_signal_status = get_order_singal()
                print("macd_delay_counter value is ",self.macd_delay_counter)
                self.macd_delay_counter = 0
                # sleep(20)

            if self.macd_up_flag:
                print("Currently in uptreand. No action required")
            if self.macd_down_flag:
                print("Currently in DownTrend. No action required")

            if self.order_signal_status == 1:
                print ("UPTREND, action need to be taken. Placing order now")
                place_buy_order()
                self.buy_order_pending = 1
            # elif self.order_signal_status == -1:
            #     print("DownTrend, action needs to be taken")
            else:
                print("No MACD UP Cross-Over Signal")
                if self.buy_order_pending == 1:
                    #if buy order not completed then call the buy order.
                    place_buy_order_market_orders_strat()
                    # if self.buy_trade_completed == 0:
                    #     place_buy_order()
                    # else:
                    #     # change the buy order completed to false , so that next order can be placed
                    #     self.buy_trade_completed = 0
                    # # print("already sent for buying, need call again")

                return


        macd_based_order_generator()

        print("Converging below ordersL:\n Buy Orders:", self.buy_orders, "\nSell Orders:",self.sell_orders)


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
                        # additional comment, dont check for relist interval, ammend immediately.
                        desired_order['price'] != order['price']):
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
            sys.stdout.write("\n\n\n")
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
