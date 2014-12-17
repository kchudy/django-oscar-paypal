from __future__ import unicode_literals
from collections import namedtuple
import logging
from decimal import Decimal as D
import urllib
import urlparse

from django.conf import settings
from django.template.defaultfilters import truncatewords, striptags
import requests
import time

from paypal.adaptive import models
from paypal import exceptions
from paypal.express import exceptions as express_exceptions

from django.utils.translation import ugettext_lazy as _


# PayPal methods
SET_EXPRESS_CHECKOUT = 'SetExpressCheckout'
GET_EXPRESS_CHECKOUT = 'GetExpressCheckoutDetails'
DO_EXPRESS_CHECKOUT = 'DoExpressCheckoutPayment'
DO_CAPTURE = 'DoCapture'
DO_VOID = 'DoVoid'
REFUND_TRANSACTION = 'RefundTransaction'
SET_CHAINED_PAYMENT = 'SetChainedPayment'

SALE, AUTHORIZATION, ORDER = 'Sale', 'Authorization', 'Order'

# The latest version of the PayPal Express API can be found here:
# https://developer.paypal.com/docs/classic/release-notes/
API_VERSION = getattr(settings, 'PAYPAL_API_VERSION', '119')

logger = logging.getLogger('paypal.express')

# Custom tuple for submitting receiver information
Receiver = namedtuple('Receiver', 'email amount is_primary')

def _format_description(description):
    if description:
        return truncatewords(striptags(description), 12)
    return ''


def _format_currency(amt):
    return amt.quantize(D('0.01'))


def _post(url, params, headers=None):
    """
    Make a POST request to the URL using the key-value pairs.  Return
    a set of key-value pairs.
    :url: URL to post to
    :params: Dict of parameters to include in post payload
    :headers: Dict of headers
    """
    if headers is None:
        headers = {}

    payload = urllib.urlencode(params)

    # Ensure correct headers are present
    if 'Content-type' not in headers:
        headers['Content-type'] = 'application/x-www-form-urlencoded'
    if 'Accepts' not in headers:
        headers['Accepts'] = 'text/plain'

    start_time = time.time()
    response = requests.post(url, payload, headers=headers)
    if response.status_code != requests.codes.ok:
        raise exceptions.PayPalError("Unable to communicate with PayPal")

    # Convert response into a simple key-value format
    pairs = {}
    for key, values in urlparse.parse_qs(response.content).items():
        pairs[key] = values[0]

    # Add audit information
    pairs['_raw_request'] = payload
    pairs['_raw_response'] = response.content
    pairs['_response_time'] = (time.time() - start_time) * 1000.0

    return pairs


def _fetch_response(params, currency, amount):
    """
    Fetch the response from PayPal and return a transaction object
    """
    # Construct return URL
    if getattr(settings, 'PAYPAL_SANDBOX_MODE', True):
        url = 'https://svcs.sandbox.paypal.com/AdaptivePayments/Pay'
    else:
        url = 'https://svcs.paypal.com/AdaptivePayments/Pay'

    # Make HTTP request
    pairs = _post(url, params, _get_auth_headers())

    print pairs

    # Record transaction data - we save this model whether the txn
    # was successful or not

    txn = models.AdaptiveTransaction(
        ack=pairs['responseEnvelope.ack'],
        raw_request=pairs['_raw_request'],
        raw_response=pairs['_raw_response'],
        response_time=pairs['_response_time'],
    )

    if txn.is_successful:
        txn.correlation_id = pairs['responseEnvelope.correlationId']
        txn.pay_key = pairs['payKey']
        txn.amount = amount
        txn.currency = currency
    else:
        # There can be more than one error, each with its own number.
        if 'L_ERRORCODE0' in pairs:
            txn.error_code = pairs['L_ERRORCODE0']
        if 'L_LONGMESSAGE0' in pairs:
            txn.error_message = pairs['L_LONGMESSAGE0']

    txn.save()

    if not txn.is_successful:
        msg = "Error %s - %s" % (txn.error_code, txn.error_message)
        logger.error(msg)
        raise exceptions.PayPalError(msg)

    return txn


def set_txn(basket, shipping_methods, currency, return_url, cancel_url, update_url=None,
            action=SALE, user=None, user_address=None, shipping_method=None,
            shipping_address=None, no_shipping=False, paypal_params=None):
    """
    Register the transaction with PayPal to get a token which we use in the
    redirect URL.  This is the 'SetExpressCheckout' from their documentation.

    There are quite a few options that can be passed to PayPal to configure
    this request - most are controlled by PAYPAL_* settings.
    """
    params = [
        ('actionType', 'PAY'),
        ('cancelUrl', cancel_url),
        ('currencyCode', currency),
        ('requestEnvelope.errorLanguage', 'en_US'),
        ('returnUrl', return_url),
    ]

    # PayPal have an upper limit on transactions.  It's in dollars which is a
    # fiddly to work with.  Lazy solution - only check when dollars are used as
    # the PayPal currency.
    amount = basket.total_incl_tax
    if currency == 'USD' and amount > 10000:
        msg = 'PayPal can only be used for orders up to 10000 USD'
        logger.error(msg)
        raise express_exceptions.InvalidBasket(_(msg))

    if amount <= 0:
        msg = 'The basket total is zero so no payment is required'
        logger.error(msg)
        raise express_exceptions.InvalidBasket(_(msg))

    receivers = _get_receivers(amount, basket.commission, basket.partner.paypal_email)

    for index, receiver in enumerate(receivers):
        params.append(('receiverList.receiver(%d).amount' % index, str(receiver.amount)))
        params.append(('receiverList.receiver(%d).email' % index, receiver.email))
        params.append(('receiverList.receiver(%d).primary' % index, 'true' if receiver.is_primary else 'false'))

    txn = _fetch_response(params, currency, amount)

    if getattr(settings, 'PAYPAL_SANDBOX_MODE', True):
        url = 'https://www.sandbox.paypal.com/webscr'
    else:
        url = 'https://www.paypal.com/webscr'

    return url + '?cmd=_ap-payment&paykey=%s' % txn.pay_key


def get_txn(token):
    """
    Fetch details of a transaction from PayPal using the token as
    an identifier.
    """
    return _fetch_response(GET_EXPRESS_CHECKOUT, {'TOKEN': token})


def do_txn(payer_id, token, amount, currency, action=SALE):
    """
    DoExpressCheckoutPayment
    """
    params = {
        'PAYERID': payer_id,
        'TOKEN': token,
        'PAYMENTREQUEST_0_AMT': amount,
        'PAYMENTREQUEST_0_CURRENCYCODE': currency,
        'PAYMENTREQUEST_0_PAYMENTACTION': action,
    }
    return _fetch_response(DO_EXPRESS_CHECKOUT, params)


def do_capture(txn_id, amount, currency, complete_type='Complete',
               note=None):
    """
    Capture payment from a previous transaction

    See https://cms.paypal.com/uk/cgi-bin/?&cmd=_render-content&content_ID=developer/e_howto_api_soap_r_DoCapture
    """
    params = {
        'AUTHORIZATIONID': txn_id,
        'AMT': amount,
        'CURRENCYCODE': currency,
        'COMPLETETYPE': complete_type,
    }
    if note:
        params['NOTE'] = note
    return _fetch_response(DO_CAPTURE, params)


def do_void(txn_id, note=None):
    params = {
        'AUTHORIZATIONID': txn_id,
    }
    if note:
        params['NOTE'] = note
    return _fetch_response(DO_VOID, params)


FULL_REFUND = 'Full'
PARTIAL_REFUND = 'Partial'
def refund_txn(txn_id, is_partial=False, amount=None, currency=None):
    params = {
        'TRANSACTIONID': txn_id,
        'REFUNDTYPE': PARTIAL_REFUND if is_partial else FULL_REFUND,
    }
    if is_partial:
        params['AMT'] = amount
        params['CURRENCYCODE'] = currency
    return _fetch_response(REFUND_TRANSACTION, params)


def _get_auth_headers():
    return {
        'X-PAYPAL-SECURITY-USERID': settings.PAYPAL_API_USERNAME,
        'X-PAYPAL-SECURITY-PASSWORD': settings.PAYPAL_API_PASSWORD,
        'X-PAYPAL-SECURITY-SIGNATURE': settings.PAYPAL_API_SIGNATURE,
        'X-PAYPAL-REQUEST-DATA-FORMAT': 'NV',
        'X-PAYPAL-RESPONSE-DATA-FORMAT': 'NV',
        'X-PAYPAL-APPLICATION-ID': settings.PAYPAL_API_APPLICATION_ID,
    }


def _get_receivers(amount, commission, partner_email):
    print 'Settings receivers for %s, %s, %s' % (amount, commission, partner_email)

    commission_amount = amount * commission / 100

    return [
        Receiver(email=partner_email, amount=amount, is_primary=True),
        Receiver(email=settings.PAYPAL_EMAIL, amount=commission_amount, is_primary=False),
    ]
