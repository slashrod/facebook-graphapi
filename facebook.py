#!/usr/bin/env python
#
# Copyright 2010 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Python client library for the Facebook Platform.

This client library is designed to support the Graph API and the
official Facebook JavaScript SDK, which is the canonical way to
implement Facebook authentication. Read more about the Graph API at
http://developers.facebook.com/docs/api. You can download the Facebook
JavaScript SDK at http://github.com/facebook/connect-js/.

If your application is using Google AppEngine's webapp framework, your
usage of this module might look like this:

user = facebook.get_user_from_cookie(self.request.cookies, key, secret)
if user:
    graph = facebook.GraphAPI(user["access_token"])
    profile = graph.get_object("me")
    friends = graph.get_connections("me", "friends")

"""

import cgi
import time
import urllib
import urllib2
import httplib
import hashlib
import hmac
import base64
import logging
import socket

# Find a JSON parser
try:
    import simplejson as json
except ImportError:
    try:
        from django.utils import simplejson as json
    except ImportError:
        import json
_parse_json = json.loads

# Find a query string parser
try:
    from urlparse import parse_qs
except ImportError:
    from cgi import parse_qs


class GraphAPI(object):
    """A client for the Facebook Graph API.

    See http://developers.facebook.com/docs/api for complete
    documentation for the API.

    The Graph API is made up of the objects in Facebook (e.g., people,
    pages, events, photos) and the connections between them (e.g.,
    friends, photo tags, and event RSVPs). This client provides access
    to those primitive types in a generic way. For example, given an
    OAuth access token, this will fetch the profile of the active user
    and the list of the user's friends:

       graph = facebook.GraphAPI(access_token)
       user = graph.get_object("me")
       friends = graph.get_connections(user["id"], "friends")

    You can see a list of all of the objects and connections supported
    by the API at http://developers.facebook.com/docs/reference/api/.

    You can obtain an access token via OAuth or by using the Facebook
    JavaScript SDK. See
    http://developers.facebook.com/docs/authentication/ for details.

    If you are using the JavaScript SDK, you can use the
    get_user_from_cookie() method below to get the OAuth access token
    for the active user from the cookie saved by the SDK.

    """
    def __init__(self, access_token=None, timeout=None, *args, **kwargs):
        self.access_token = access_token
        self.timeout = timeout
        self.max_pages = kwargs.pop("max_pages", 3)

    def get_object(self, id, **args):
        """Fetchs the given object from the graph."""
        return self.request(id, args)

    def get_objects(self, ids, **args):
        """Fetchs all of the given object from the graph.

        We return a map from ID to object. If any of the IDs are
        invalid, we raise an exception.
        """
        args["ids"] = ",".join(ids)
        return self.request("", args)

    def get_connections(self, id, connection_name, **args):
        """Fetchs the connections for given object."""
        as_generator = args.pop("as_generator", False)
        if as_generator:
            return self._paginator(id + "/" + connection_name, args)
        return self.request(id + "/" + connection_name, args)

    def put_object(self, parent_object, connection_name, **data):
        """Writes the given object to the graph, connected to the given parent.

        For example,

            graph.put_object("me", "feed", message="Hello, world")

        writes "Hello, world" to the active user's wall. Likewise, this
        will comment on a the first post of the active user's feed:

            feed = graph.get_connections("me", "feed")
            post = feed["data"][0]
            graph.put_object(post["id"], "comments", message="First!")

        See http://developers.facebook.com/docs/api#publishing for all
        of the supported writeable objects.

        Certain write operations require extended permissions. For
        example, publishing to a user's feed requires the
        "publish_actions" permission. See
        http://developers.facebook.com/docs/publishing/ for details
        about publishing permissions.

        """
        assert self.access_token, "Write operations require an access token"
        return self.request(parent_object + "/" + connection_name,
                            post_args=data)

    def put_wall_post(self, message, attachment={}, profile_id="me"):
        """Writes a wall post to the given profile's wall.

        We default to writing to the authenticated user's wall if no
        profile_id is specified.

        attachment adds a structured attachment to the status message
        being posted to the Wall. It should be a dictionary of the form:

            {"name": "Link name"
             "link": "http://www.example.com/",
             "caption": "{*actor*} posted a new review",
             "description": "This is a longer description of the attachment",
             "picture": "http://www.example.com/thumbnail.jpg"}

        """
        return self.put_object(profile_id, "feed", message=message,
                               **attachment)

    def put_comment(self, object_id, message):
        """Writes the given comment on the given post."""
        return self.put_object(object_id, "comments", message=message)

    def put_like(self, object_id):
        """Likes the given post."""
        return self.put_object(object_id, "likes")

    def delete_object(self, id):
        """Deletes the object with the given ID from the graph."""
        self.request(id, post_args={"method": "delete"})

    def delete_request(self, user_id, request_id):
        """Deletes the Request with the given ID for the given user."""
        conn = httplib.HTTPSConnection('graph.facebook.com')

        url = '/%s_%s?%s' % (
            request_id,
            user_id,
            urllib.urlencode({'access_token': self.access_token}),
        )
        conn.request('DELETE', url)
        response = conn.getresponse()
        data = response.read()

        response = _parse_json(data)
        # Raise an error if we got one, but don't not if Facebook just
        # gave us a Bool value
        if (response and isinstance(response, dict) and response.get("error")):
            raise raise_error(response), response

        conn.close()

    def put_photo(self, image, message=None, album_id=None, **kwargs):
        """Uploads an image using multipart/form-data.

        image=File like object for the image
        message=Caption for your image
        album_id=None posts to /me/photos which uses or creates and uses
        an album for your application.

        """
        object_id = album_id or "me"
        #it would have been nice to reuse self.request;
        #but multipart is messy in urllib
        post_args = {
            'access_token': self.access_token,
            'source': image,
            'message': message,
        }
        post_args.update(kwargs)
        content_type, body = self._encode_multipart_form(post_args)
        req = urllib2.Request(("https://graph.facebook.com/%s/photos" %
                               object_id),
                              data=body)
        req.add_header('Content-Type', content_type)
        try:
            data = urllib2.urlopen(req).read()
        #For Python 3 use this:
        #except urllib2.HTTPError as e:
        except urllib2.HTTPError, e:
            data = e.read()  # Facebook sends OAuth errors as 400, and urllib2
                             # throws an exception, we want a GraphAPIError
        try:
            response = _parse_json(data)
            # Raise an error if we got one, but don't not if Facebook just
            # gave us a Bool value
            if (response and isinstance(response, dict) and
                    response.get("error")):
                raise raise_error(response), response
        except ValueError:
            response = data

        return response

    # based on: http://code.activestate.com/recipes/146306/
    def _encode_multipart_form(self, fields):
        """Encode files as 'multipart/form-data'.

        Fields are a dict of form name-> value. For files, value should
        be a file object. Other file-like objects might work and a fake
        name will be chosen.

        Returns (content_type, body) ready for httplib.HTTP instance.

        """
        BOUNDARY = '----------ThIs_Is_tHe_bouNdaRY_$'
        CRLF = '\r\n'
        L = []
        for (key, value) in fields.items():
            logging.debug("Encoding %s, (%s)%s" % (key, type(value), value))
            if not value:
                continue
            L.append('--' + BOUNDARY)
            if hasattr(value, 'read') and callable(value.read):
                filename = getattr(value, 'name', '%s.jpg' % key)
                L.append(('Content-Disposition: form-data;'
                          'name="%s";'
                          'filename="%s"') % (key, filename))
                L.append('Content-Type: image/jpeg')
                value = value.read()
                logging.debug(type(value))
            else:
                L.append('Content-Disposition: form-data; name="%s"' % key)
            L.append('')
            if isinstance(value, unicode):
                logging.debug("Convert to ascii")
                value = value.encode('ascii')
            L.append(value)
        L.append('--' + BOUNDARY + '--')
        L.append('')
        body = CRLF.join(L)
        content_type = 'multipart/form-data; boundary=%s' % BOUNDARY
        return content_type, body

    def prepare_url_with_post_data(self, path, args=None, post_args=None):
        """Prepare a Graph API URL with the given path and arguments"""
        args = args or {}

        if self.access_token:
            if post_args is not None:
                post_args["access_token"] = self.access_token
            else:
                args["access_token"] = self.access_token
        post_data = None if post_args is None else urllib.urlencode(post_args)
        prefix = "https://graph.facebook.com/"
        url = prefix + path + "?" + urllib.urlencode(args)

        return url, post_data

    def _paginator(self, path, args=None):
        """Creates a paginator with the given path in the Graph API."""
        url, post_data = self.prepare_url_with_post_data(path, args)
        pages_read = 0
        while url and pages_read < self.max_pages:
            api_responses, url = self._raw_request(url)
            pages_read += 1
            yield api_responses, url
        return

    def request(self, path, args=None, post_args=None):
        """Fetches the given path in the Graph API.

        We translate args to a valid query string. If post_args is
        given, we send a POST request to the given path with the given
        arguments.

        """
        url, post_data = self.prepare_url_with_post_data(path, args, post_args)
        response, next_url = self._raw_request(url, post_data)
        return response

    def _raw_request(self, url, post_data=None):
        """Fetches the given raw Graph API URL.

        Perform HTTP request with the given URL and POST data, if any.
        """
        try:
            file = urllib2.urlopen(url, post_data, timeout=self.timeout)
        except urllib2.HTTPError, e:
            response = _parse_json(e.read())
            raise GraphAPIError(response)
        except TypeError:
            # Timeout support for Python <2.6
            if self.timeout:
                socket.setdefaulttimeout(self.timeout)
            file = urllib2.urlopen(url, post_data)
        try:
            fileInfo = file.info()
            if fileInfo.maintype == 'text':
                response = _parse_json(file.read())
            elif fileInfo.maintype == 'image':
                mimetype = fileInfo['content-type']
                response = {
                    "data": file.read(),
                    "mime-type": mimetype,
                    "url": file.url,
                }
            else:
                raise raise_error('Maintype was not text or image')
        finally:
            file.close()
        if response and isinstance(response, dict) and response.get("error"):
            raise GraphAPIError(response["error"]["type"],
                                response["error"]["message"])

        next_url = response.get('paging', {}).get('next')
        data = response.get('data')
        if data is not None:
            response = data
        return response, next_url

    def fql(self, query, args=None, post_args=None):
        """FQL query.

        Example query: "SELECT affiliations FROM user WHERE uid = me()"

        """
        args = args or {}
        if self.access_token:
            if post_args is not None:
                post_args["access_token"] = self.access_token
            else:
                args["access_token"] = self.access_token
        post_data = None if post_args is None else urllib.urlencode(post_args)

        """Check if query is a dict and
           use the multiquery method
           else use single query
        """
        if not isinstance(query, basestring):
            args["queries"] = query
            fql_method = 'fql.multiquery'
        else:
            args["query"] = query
            fql_method = 'fql.query'

        args["format"] = "json"

        try:
            file = urllib2.urlopen("https://api.facebook.com/method/" +
                                   fql_method + "?" + urllib.urlencode(args),
                                   post_data, timeout=self.timeout)
        except TypeError:
            # Timeout support for Python <2.6
            if self.timeout:
                socket.setdefaulttimeout(self.timeout)
            file = urllib2.urlopen("https://api.facebook.com/method/" +
                                   fql_method + "?" + urllib.urlencode(args),
                                   post_data)

        try:
            content = file.read()
            response = _parse_json(content)
            #Return a list if success, return a dictionary if failed
            if type(response) is dict and "error_code" in response:
                raise raise_error(response), response
        except Exception, e:
            raise e
        finally:
            file.close()

        return response

    def extend_access_token(self, app_id, app_secret):
        """
        Extends the expiration time of a valid OAuth access token. See
        <https://developers.facebook.com/roadmap/offline-access-removal/
        #extend_token>

        """
        args = {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "fb_exchange_token",
            "fb_exchange_token": self.access_token,
        }
        response = urllib.urlopen("https://graph.facebook.com/oauth/"
                                  "access_token?" +
                                  urllib.urlencode(args)).read()
        query_str = parse_qs(response)
        if "access_token" in query_str:
            result = {"access_token": query_str["access_token"][0]}
            if "expires" in query_str:
                result["expires"] = query_str["expires"][0]
            return result
        else:
            response = json.loads(response)
            raise raise_error(response), response


class GraphAPIError(Exception):
    def __init__(self, result):
        #Exception.__init__(self, message)
        #self.type = type
        self.result = result
        try:
            self.type = result["error_code"]
        except:
            self.type = ""

        # OAuth 2.0 Draft 10
        try:
            self.message = result["error_description"]
        except:
            # OAuth 2.0 Draft 00
            try:
                self.message = result["error"]["message"]
            except:
                # REST server style
                try:
                    self.message = result["error_msg"]
                except:
                    self.message = result

        Exception.__init__(self, self.message)

class OAuthError(GraphAPIError):
    """
        OAuth Error. Reauthenticate the session
    """
    pass

class ServerError(GraphAPIError):
    """
        Server side error, hold on and retry later
    """
    pass

class UserError(GraphAPIError):
    """
        User has not either granted a permission or it has removed it
    """
    pass

class AppOAuthError(OAuthError):
    """
        User removed the app fom it settings

    """
    pass

class UserOAuthError(OAuthError):
    """
        User checkpointed. He needs to log onto www.facebook.com or
        m.facebook.com
    """
    pass

class PasswordOAuthError(OAuthError):
    """
        Password Changed on Facebook

    """
    pass

class ExpiredOAuthError(OAuthError):
    """
        
        Token expired and a new one needs to be requested

    """
    pass

class UnconfirmedOAuthError(OAuthError):
    """
    
        User needs to log onto www.facebook.com, m.facebook.com
    
    """
    pass

class InvalidOAuthError(OAuthError):
    """
        Invalid Token and a neww needs to be requested
    
    """
    pass


def raise_error(response):
    
    code = response['error']['code']
    error_subcode = None

    if code in (190, 102):
        error_subcode = response['error']['error_subcode']

    exceptions = { 
            190: 
            {
                458 : AppOAuthError,
                459 : UserOAuthError,
                460 : PasswordOAuthError,
                463 : ExpiredOAuthError,
                464 : UnconfirmedOAuthError,
                467 : InvalidOAuthError
                },
            1 : ServerError,
            2 : ServerError,
            4 : ServerError,
            17 : ServerError,
            10 : UserError
            }

    exceptions[102] = exceptions[190]

    if error_subcode:
        return exceptions[code][error_subcode]

    return exceptions[code]







def get_user_from_cookie(cookies, app_id, app_secret):
    """Parses the cookie set by the official Facebook JavaScript SDK.

    cookies should be a dictionary-like object mapping cookie names to
    cookie values.

    If the user is logged in via Facebook, we return a dictionary with
    the keys "uid" and "access_token". The former is the user's
    Facebook ID, and the latter can be used to make authenticated
    requests to the Graph API. If the user is not logged in, we
    return None.

    Download the official Facebook JavaScript SDK at
    http://github.com/facebook/connect-js/. Read more about Facebook
    authentication at
    http://developers.facebook.com/docs/authentication/.

    """
    cookie = cookies.get("fbsr_" + app_id, "")
    if not cookie:
        return None
    parsed_request = parse_signed_request(cookie, app_secret)
    if not parsed_request:
        return None
    try:
        result = get_access_token_from_code(parsed_request["code"], "",
                                            app_id, app_secret)
    except GraphAPIError:
        return None
    result["uid"] = parsed_request["user_id"]
    return result


def parse_signed_request(signed_request, app_secret):
    """ Return dictionary with signed request data.

    We return a dictionary containing the information in the
    signed_request. This includes a user_id if the user has authorised
    your application, as well as any information requested.

    If the signed_request is malformed or corrupted, False is returned.

    """
    try:
        encoded_sig, payload = map(str, signed_request.split('.', 1))

        sig = base64.urlsafe_b64decode(encoded_sig + "=" *
                                       ((4 - len(encoded_sig) % 4) % 4))
        data = base64.urlsafe_b64decode(payload + "=" *
                                        ((4 - len(payload) % 4) % 4))
    except IndexError:
        # Signed request was malformed.
        return False
    except TypeError:
        # Signed request had a corrupted payload.
        return False

    data = _parse_json(data)
    if data.get('algorithm', '').upper() != 'HMAC-SHA256':
        return False

    # HMAC can only handle ascii (byte) strings
    # http://bugs.python.org/issue5285
    app_secret = app_secret.encode('ascii')
    payload = payload.encode('ascii')

    expected_sig = hmac.new(app_secret,
                            msg=payload,
                            digestmod=hashlib.sha256).digest()
    if sig != expected_sig:
        return False

    return data


def auth_url(app_id, canvas_url, perms=None, **kwargs):
    url = "https://www.facebook.com/dialog/oauth?"
    kvps = {'client_id': app_id, 'redirect_uri': canvas_url}
    if perms:
        kvps['scope'] = ",".join(perms)
    kvps.update(kwargs)
    return url + urllib.urlencode(kvps)

def get_access_token_from_code(code, redirect_uri, app_id, app_secret):
    """Get an access token from the "code" returned from an OAuth dialog.

    Returns a dict containing the user-specific access token and its
    expiration date (if applicable).

    """
    args = {
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": app_id,
        "client_secret": app_secret,
    }
    # We would use GraphAPI.request() here, except for that the fact
    # that the response is a key-value pair, and not JSON.
    response = urllib.urlopen("https://graph.facebook.com/oauth/access_token" +
                              "?" + urllib.urlencode(args)).read()
    query_str = parse_qs(response)
    if "access_token" in query_str:
        result = {"access_token": query_str["access_token"][0]}
        if "expires" in query_str:
            result["expires"] = query_str["expires"][0]
        return result
    else:
        response = json.loads(response)
        raise raise_error(response), response


def get_app_access_token(app_id, app_secret):
    """Get the access_token for the app.

    This token can be used for insights and creating test users.

    app_id = retrieved from the developer page
    app_secret = retrieved from the developer page

    Returns the application access_token.

    """
    # Get an app access token
    args = {'grant_type': 'client_credentials',
            'client_id': app_id,
            'client_secret': app_secret}

    file = urllib2.urlopen("https://graph.facebook.com/oauth/access_token?" +
                           urllib.urlencode(args))

    try:
        result = file.read().split("=")[1]
    finally:
        file.close()

    return result

def get_long_lived_access_token(app_id, app_secret, short_lived_token):
    """Get the access_token for the app.

    This token can be used for insights and creating test users.
    It uses a live, authorized token to generate a long lived one

    app_id = retrieved from the developer page
    app_secret = retrieved from the developer page
    short_lived_token = retrieved after successfull login, should be valid

    Returns the long_live  access_token.

    """
    # Get an app access token
    args = {
            'grant_type': 'fb_exchange_token',
            'client_id': app_id,
            'client_secret': app_secret,
            'fb_exchange_token': short_lived_token
            }

    f = urllib2.urlopen("https://graph.facebook.com/oauth/access_token?" +
                           urllib.urlencode(args))


    try:
        result = f.read().split("=")[1].split("&")[0]
    finally:
        f.close()

    return result

def debug_access_token(input_token, access_token):
    """Get debug information for an access_token

    input_token: the access token you want to get information about
    access_token: your [app access token][10] or a valid user access token
                from a developer of the app. In fact this is
                app_access_token returned by get_app_access_token(app_id,
                app_secret)
    
    returns: 
            {
                "data": {
                    "app_id": 138483919580948, 
                    "application": "Social Cafe", 
                    "expires_at": 1352419328, 
                    "is_valid": true, 
                    "issued_at": 1347235328, 
                    "metadata": {
                        "sso": "iphone-safari"
                    }, 
                    "scopes": [
                        "email", 
                        "publish_actions"
                    ], 
                    "user_id": 1207059
                }
            }
    """
    args = {
            'input_token': input_token,
            'access_token': access_token,
            }

    response = urllib.urlopen("https://graph.facebook.com/debug_token" +
                              "?" + urllib.urlencode(args)).read()

    response = json.loads(response)
    return response


def valid_access_token(input_token, access_token):
    """
        Return False if access token not valid
        Return (True, expiration_time in unix time, expiration time human
        readable) if still valid
    """

    response = debug_access_token(input_token, access_token)
    response = response['data']


    if response["is_valid"]:
        import time
        return True, response["expires_at"], int(time.ctime(int(response["expires_at"])))
    else:
        return False
