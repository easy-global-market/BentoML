# Copyright 2019 Atalaya Tech, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import sys
from functools import partial

from dependency_injector.wiring import Provide, inject
from flask import Flask, Response, Request, jsonify, make_response, request, send_from_directory
from google.protobuf.json_format import MessageToJson
from werkzeug.exceptions import BadRequest, NotFound

from bentoml import config
from bentoml import BentoService
from bentoml.configuration import get_debug_mode
from bentoml.configuration.containers import BentoMLContainer
from bentoml.exceptions import BentoMLException
from bentoml.marshal.utils import DataLoader
from bentoml.server.instruments import InstrumentMiddleware
from bentoml.server.open_api import get_open_api_spec_json
from bentoml.service import InferenceAPI
from bentoml.tracing import get_tracer

import requests
from datetime import datetime, timezone, timedelta
import pytz
import numpy as np
import torch
import json
import pickle
import csv
from urllib.parse import quote_plus 

import uuid

CONTENT_TYPE_LATEST = str("text/plain; version=0.0.4; charset=utf-8")

feedback_logger = logging.getLogger("bentoml.feedback")
logger = logging.getLogger(__name__)


DEFAULT_INDEX_HTML = '''\
<!DOCTYPE html>
<head>
  <link rel="stylesheet" type="text/css" href="static_content/main.css">
  <link rel="stylesheet" type="text/css" href="static_content/readme.css">
  <link rel="stylesheet" type="text/css" href="static_content/swagger-ui.css">
</head>
<body>
  <div id="tab">
    <button
      class="tabLinks active"
      onclick="openTab(event, 'swagger_ui_container')"
      id="defaultOpen"
    >
      Swagger UI
    </button>
    <button class="tabLinks" onclick="openTab(event, 'markdown_readme')">
      ReadMe
    </button>
  </div>
  <script>
    function openTab(evt, tabName) {{
      // Declare all variables
      var i, tabContent, tabLinks;
      // Get all elements with class="tabContent" and hide them
      tabContent = document.getElementsByClassName("tabContent");
      for (i = 0; i < tabContent.length; i++) {{
        tabContent[i].style.display = "none";
      }}

      // Get all elements with class="tabLinks" and remove the class "active"
      tabLinks = document.getElementsByClassName("tabLinks");
      for (i = 0; i < tabLinks.length; i++) {{
        tabLinks[i].className = tabLinks[i].className.replace(" active", "");
      }}

      // Show the current tab, and add an "active" class to the button that opened the
      // tab
      document.getElementById(tabName).style.display = "block";
      evt.currentTarget.className += " active";
    }}
  </script>
  <div id="markdown_readme" class="tabContent"></div>
  <script src="static_content/marked.min.js"></script>
  <script>
    var markdownContent = marked(`{readme}`);
    var element = document.getElementById('markdown_readme');
    element.innerHTML = markdownContent;
  </script>
  <div id="swagger_ui_container" class="tabContent" style="display: block"></div>
  <script src="static_content/swagger-ui-bundle.js"></script>
  <script>
      SwaggerUIBundle({{
          url: '{url}',
          dom_id: '#swagger_ui_container'
      }})
  </script>
</body>
'''

SWAGGER_HTML = '''\
<!DOCTYPE html>
<head>
  <link rel="stylesheet" type="text/css" href="static_content/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui-container"></div>
  <script src="static_content/swagger-ui-bundle.js"></script>
  <script>
      SwaggerUIBundle({{
          url: '{url}',
          dom_id: '#swagger-ui-container'
      }})
  </script>
</body>
'''


def _request_to_json(req):
    """
    Return request data for log prediction
    """
    if req.content_type == "application/json":
        return req.get_json()

    return {}



def log_exception(exc_info):
    """
    Logs an exception.  This is called by :meth:`handle_exception`
    if debugging is disabled and right before the handler is called.
    The default implementation logs the exception as error on the
    :attr:`logger`.
    """
    logger.error(
        "Exception on %s [%s]", request.path, request.method, exc_info=exc_info
    )


class BentoAPIServer:
    """
    BentoAPIServer creates a REST API server based on APIs defined with a BentoService
    via BentoService#get_service_apis call. Each InferenceAPI will become one
    endpoint exposed on the REST server, and the RequestHandler defined on each
    InferenceAPI object will be used to handle Request object before feeding the
    request data into a Service API function
    """

    @inject
    def __init__(
        self,
        bento_service: BentoService,
        app_name: str = None,
        enable_swagger: bool = Provide[
            BentoMLContainer.config.api_server.enable_swagger
        ],
        enable_metrics: bool = Provide[
            BentoMLContainer.config.api_server.enable_metrics
        ],
        enable_feedback: bool = Provide[
            BentoMLContainer.config.api_server.enable_feedback
        ],
        request_header_flag: str = Provide[
            BentoMLContainer.config.marshal_server.request_header_flag
        ],
    ):
        app_name = bento_service.name if app_name is None else app_name

        self.bento_service = bento_service
        self.app = Flask(app_name, static_folder=None)
        self.static_path = self.bento_service.get_web_static_content_path()
        self.enable_swagger = enable_swagger
        self.enable_metrics = enable_metrics
        self.enable_feedback = enable_feedback
        self.request_header_flag = request_header_flag

        # NGSI-LD configuration parameters
        self.ngsild_cb_url = config('ngsild').get('cb_url')
        self.ngsild_at_context = config('ngsild').get('at_context')
        self.ngsild_access_token = config('ngsild').get('access_token')
        self.ngsild_ml_model_input = config('ngsild').get('ml_model_input')
        self.ngsild_ml_model_entity_input_type = config('ngsild').get('ml_model_entity_input_type')
        self.ngsild_ml_model_target_entity = config('ngsild').get('ml_model_target_entity')

        self.swagger_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'static_content'
        )

        for middleware in (InstrumentMiddleware,):
            self.app.wsgi_app = middleware(self.app.wsgi_app, self.bento_service)

        self.setup_routes()

    def start(self, port: int, host: str = "127.0.0.1"):
        """
        Start an REST server at the specific port on the instance or parameter.
        """
        # Bentoml api service is not thread safe.
        # Flask dev server enabled threaded by default, disable it.
        self.app.run(
            host=host,
            port=port,
            threaded=False,
            debug=get_debug_mode(),
            use_reloader=False,
        )

    @staticmethod
    def static_serve(static_path, file_path):
        """
        The static files route for BentoML API server
        """
        try:
            return send_from_directory(static_path, file_path)
        except NotFound:
            return send_from_directory(
                os.path.join(static_path, file_path), "index.html"
            )

    @staticmethod
    def index_view_func(static_path):
        """
        The index route for BentoML API server
        """
        return send_from_directory(static_path, 'index.html')

    def default_index_view_func(self):
        """
        The default index view for BentoML API server. This includes the readme
        generated from docstring and swagger UI
        """
        if not self.enable_swagger:
            return Response(
                response="Swagger is disabled", status=404, mimetype="text/html"
            )
        return Response(
            response=DEFAULT_INDEX_HTML.format(
                url='docs.json', readme=self.bento_service.__doc__
            ),
            status=200,
            mimetype="text/html",
        )

    def swagger_ui_func(self):
        """
        The swagger UI route for BentoML API server
        """
        if not self.enable_swagger:
            return Response(
                response="Swagger is disabled", status=404, mimetype="text/html"
            )
        return Response(
            response=SWAGGER_HTML.format(url='docs.json'),
            status=200,
            mimetype="text/html",
        )

    @staticmethod
    def swagger_static(static_path, filename):
        """
        The swagger static files route for BentoML API server
        """
        return send_from_directory(static_path, filename)

    @staticmethod
    def docs_view_func(bento_service):
        docs = get_open_api_spec_json(bento_service)
        return jsonify(docs)

    @staticmethod
    def healthz_view_func():
        """
        Health check for BentoML API server.
        Make sure it works with Kubernetes liveness probe
        """
        return Response(response="\n", status=200, mimetype="text/plain")

    @staticmethod
    def metadata_json_func(bento_service):
        bento_service_metadata = bento_service.get_bento_service_metadata_pb()
        return jsonify(MessageToJson(bento_service_metadata))

    def metrics_view_func(self):
        # noinspection PyProtectedMember
        from prometheus_client import generate_latest

        return generate_latest()

    @staticmethod
    def feedback_view_func(bento_service):
        """
        User send feedback along with the request_id. It will be stored is feedback logs
        ready for further process.
        """
        data = request.get_json()

        if not data:
            raise BadRequest("Failed parsing feedback JSON data")

        if "request_id" not in data:
            raise BadRequest("Missing 'request_id' in feedback JSON data")

        data["service_name"] = bento_service.name
        data["service_version"] = bento_service.version
        feedback_logger.info(data)
        return "success"

    def setup_routes(self):
        """
        Setup routes for bento model server, including:

        /               Index Page
        /docs           Swagger UI
        /healthz        Health check ping
        /feedback       Submitting feedback
        /metrics        Prometheus metrics endpoint
        /metadata       BentoService Artifact Metadata

        And user defined InferenceAPI list into flask routes, e.g.:
        /classify
        /predict
        """
        if self.static_path:
            # serve static files for any given path
            # this will also serve index.html from directory /any_path/
            # for path as /any_path/
            self.app.add_url_rule(
                "/<path:file_path>",
                "static_proxy",
                partial(self.static_serve, self.static_path),
            )
            # serve index.html from the directory /any_path
            # for path as /any_path/index
            self.app.add_url_rule(
                "/<path:file_path>/index",
                "static_proxy2",
                partial(self.static_serve, self.static_path),
            )
            # serve index.html from root directory for path as /
            self.app.add_url_rule(
                "/", "index", partial(self.index_view_func, self.static_path)
            )
        else:
            self.app.add_url_rule("/", "index", self.default_index_view_func)

        self.app.add_url_rule("/docs", "swagger", self.swagger_ui_func)
        self.app.add_url_rule(
            "/static_content/<path:filename>",
            "static_content",
            partial(self.swagger_static, self.swagger_path),
        )
        self.app.add_url_rule(
            "/docs.json", "docs", partial(self.docs_view_func, self.bento_service)
        )
        self.app.add_url_rule("/healthz", "healthz", self.healthz_view_func)
        self.app.add_url_rule(
            "/metadata",
            "metadata",
            partial(self.metadata_json_func, self.bento_service),
        )

        if self.enable_metrics:
            self.app.add_url_rule("/metrics", "metrics", self.metrics_view_func)

        if self.enable_feedback:
            self.app.add_url_rule(
                "/feedback",
                "feedback",
                partial(self.feedback_view_func, self.bento_service),
                methods=["POST"],
            )

        self.setup_bento_service_api_routes()

        self.app.add_url_rule(
            rule="/ngsi-ld/ml/processing",
            endpoint="processing",
            view_func=self.handle_ml_processing,
            methods=["POST"]
        )

        self.app.add_url_rule(
            rule="/ngsi-ld/ml/predict",
            endpoint="ml-predict",
            view_func=self.handle_ml_predict,
            methods=["POST"]
        )


    def handle_ml_processing(self):
        """
        Handle receipt of a notification from subscription to
        MLProcessing entities. It indicates a new Application is interested
        in using this MLModel.

        On receipt of this notification, the information on where (specifically, on which
        entity) to find the input data for prediction is retrieved, and a subscription
        is created to be notified when some input data of the entity changes.
        The input data are configurable, i.e. must be provided through environment variables
        when executing the BENTO model.

        The notification received looks like:

        {
            "id": "urn:ngsi-ld:Notification:933978d4-deab-48d5-9d63-ee532602fe73",
            "type": "Notification",
            "subscriptionId": "urn:ngsi-ld:Subscription:MLModel:flow:3M:predict:71dba318-2989-4c76-a22c-52a53f04759b",
            "notifiedAt": "2022-03-09T15:57:02.034465857Z",
            "data": [
                {
                "id": "urn:ngsi-ld:MLProcessing:4bbb2b09-ad6c-4fb9-8f40-8d37e4cddd3a",
                "type": "MLProcessing",
                "entityID": {
                    "type": "Property",
                    "createdAt": "2022-03-09T15:57:01.928429608Z",
                    "value": "urn:ngsi-ld:River:Siagne:6170cc52-0fb1-4ac2-b429-f02acbd2001b"
                },
                "@context": [
                    "https://raw.githubusercontent.com/easy-global-market/ngsild-api-data-models/master/mlaas/jsonld-contexts/mlaas-precipitation-contexts.jsonld"
                ]
                }
            ]
        }

        We need to:
        * Extract the entityID from the notification
        * Finally create a subscription to the change of this data. 
        """
        logger.info("Received a notification for a MLProcessing entity")

        # Some generic configuration
        access_token = self.ngsild_access_token
        headers = {
            'Authorization': 'Bearer ' + access_token,
            'Content-Type': 'application/ld+json'
        }
        URL_SUBSCRIPTION = self.ngsild_cb_url + '/ngsi-ld/v1/subscriptions/'
        SUBSCRIPTION_INPUT_DATA = 'urn:ngsi-ld:Subscription:input:data:'+str(uuid.uuid4())
        AT_CONTEXT = [ self.ngsild_at_context ]
        ENTITY_INPUT_TYPE = self.ngsild_ml_model_entity_input_type
        ATTRIBUTE_INPUT_DATA = self.ngsild_ml_model_input.split(',')
        logger.info('ATTRIBUTE_INPUT_DATA: %s', ATTRIBUTE_INPUT_DATA)

        # Get the POST data
        mlprocessing_notification = request.get_json()
        logger.info('Notification received: %s', mlprocessing_notification)

        # Getting the EntityID where to get input data
        ENTITY_INPUT_DATA = mlprocessing_notification['data'][0]['entityID']['value']
        logger.info('type of ENTITY_INPUT_DATA from conf: %s', type(ENTITY_INPUT_DATA))
        logger.info('ENTITY_INPUT_DATA: %s', ENTITY_INPUT_DATA)

        # Only for testing with postman mock server, replace
        # 'uri': request.url_root + '/ngsi-ld/ml/predict'
        # by postman mock server id
        # 'uri': 'https://0ba2eb3a-2ff5-4a72-9a6f-f430f9f41ad3.mock.pstmn.io/ngsi-ld/ml/predict' 

        json_ = {
            '@context': AT_CONTEXT,
            'id': SUBSCRIPTION_INPUT_DATA,
            'type': 'Subscription',
            'entities': [
                {
                    'id': ENTITY_INPUT_DATA,
                    'type': ENTITY_INPUT_TYPE
                }
            ],
            'watchedAttributes': ATTRIBUTE_INPUT_DATA,
            'notification': {
                'endpoint': {
                    # 'uri': request.url_root + '/ngsi-ld/ml/predict',
                    'uri': 'https://0ba2eb3a-2ff5-4a72-9a6f-f430f9f41ad3.mock.pstmn.io/ngsi-ld/ml/predict',
                    'accept': 'application/json'
                },
                'attributes': ATTRIBUTE_INPUT_DATA
            }
        }

        logger.info('json of request: %s', json_)

        # Create the subscription
        r = requests.post(URL_SUBSCRIPTION, json=json_, headers=headers)
        logger.info('request status_code for creation of the Subscription: %s', r.status_code)
        if r.status_code not in [200, 201]:
            logger.info('Error: %s', r.json())

        # Finally, respond to the initial received request (notification) with a 200        
        response = make_response(
            '',
            200,
        )
        logger.info("Returning from MLProcessing handling")
        return response


    def handle_ml_predict(self):
        """
        Handle the request for a prediction. The request is actually a NGSI-LD
        notification of the change of a particular property of an NGSI-LD
        Entity.

        The notification received looks like:

        {
            'id': 'urn:ngsi-ld:Notification:cc231a15-d220-403c-bfc6-ad60bc49466f',
            'type': 'Notification',
            'subscriptionId': 'urn:ngsi-ld:Subscription:input:data:2c30fa86-a25c-4191-8311-8954294e92b3',
            'notifiedAt': '2021-05-04T06:45:32.83178Z',
            'data': [
                {
                    'id': 'urn:ngsi-ld:River:Siagne:6170cc52-0fb1-4ac2-b429-f02acbd2001b',
                    'type': 'River',
                    'precipitation': {
                        'type': 'Property',
                        'createdAt': '2021-05-04T06:45:32.674520Z',
                        'value': 2.2,
                        'observedAt': '2021-05-04T06:35:22.000Z',
                        'unitCode': 'MMT'
                    },
                    '@context': [
                        'https://raw.githubusercontent.com/easy-global-market/ngsild-api-data-models/master/mlaas/jsonld-contexts/mlaas-precipitation-compound.jsonld'
                    ]
                }
            ]
        }

        We need to:
        * Extract the input_data from the NGSI-LD Notification
          (although, because we need here more than one attribute, i.e.
          precipitation and temperature) it has to be hardcoded/configurable.
        * Get 3 past months (with starting date = current date) of data
          for precipitation AND temperature. Data must be aggregated by 30 days.
        * Run a prediction with this data (Given the algorithm chosen, it implies creating
          a data sequence and making a call to predict)
        * Update the target entity with the prediction(s)
          ... To be continued !        
        """
        logger.info("-- Entering handle_ml_predict ...")

        # Some generic configuration
        access_token = self.ngsild_access_token
        headers = {
            'Authorization': 'Bearer ' + access_token,
            'Content-Type': 'application/ld+json'
        }
        
        RIVER_SIAGNE_ID = 'urn:ngsi-ld:River:Siagne'
        WEATHER_UUID = 'urn:ngsi-ld:WeatherObserved:Pegomas'
        URL_ENTITIES = self.ngsild_cb_url + '/ngsi-ld/v1/entities/'
        URL_TEMPORAL = self.ngsild_cb_url + '/ngsi-ld/v1/temporal/entities/'
        AT_CONTEXT = [ self.ngsild_at_context ]
        TARGET_ENTITY = self.ngsild_ml_model_target_entity
        FLOW = 'flow'
        PRECIPITATION = 'https://uri.fiware.org/ns/data-models#precipitation'
        TEMPERATURE = 'https://uri.fiware.org/ns/data-models#temperature'
        

        # Get the POST data
        # We actually don't care about it, as we actually need to get
        # data from different sources, i.e. not only precipitation but
        # temperature as well.
        # This notification just acts as atrigger
        input_data_notification = request.get_json()
        logger.info('input_data received from notification: %s', input_data_notification)

        # We initialise the hydrological model first.
        # We need to retrieve 6 months of data (aggregated as 6 values)
        # for (flow, precipitation, temperature). We then store these
        # values as a csv file, to be read by the hydrological model.
        
        # Get the data 6 months in the past
        _180DaysBefore = datetime.now(timezone.utc) - timedelta(days=179)
        _180DaysBeforeStr = _180DaysBefore.strftime("%Y-%m-%dT%H:%M:%SZ")

        # To predict next three months of precipitation/temperature
        # We get the current date - 90 days, then perform get temporal
        # with 30 days aggregation to retrieve THREE values of precipitation
        # and temperature from WeatherObserved Pegomas
        # The ML model being used here predict the (precipitation, temperature) 
        # of the next month from the three previous month.
        _90DaysBefore = datetime.now(timezone.utc) - timedelta(days=90)
        _90DaysBeforeStr = _90DaysBefore.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # precipitation (Aggregation SUM)
        query = '?attrs=https%3A%2F%2Furi.fiware.org%2Fns%2Fdata-models%23precipitation&timerel=after&time=' \
                +_90DaysBeforeStr+'&timeBucket=30 day&aggregate=SUM'
        r = requests.get(URL_TEMPORAL+WEATHER_UUID+query, headers=headers)
        values = r.json()['https://uri.fiware.org/ns/data-models#precipitation'][0]['values']
        precipitation = []
        for item in values:
            precipitation.append(round(item[0], 3))
        # For whatever reason, 4 values are returned by Stellio for 90 days and time bucket
        # of 30 days ??
        # Remove last value if it exist
        if len(precipitation) == 4:
            precipitation.pop()
        
        # temperature (Aggregation AVG)
        query = '?attrs=https%3A%2F%2Furi.fiware.org%2Fns%2Fdata-models%23temperature&timerel=after&time=' \
                +_90DaysBeforeStr+'&timeBucket=30 day&aggregate=AVG'
        r = requests.get(URL_TEMPORAL+WEATHER_UUID+query, headers=headers)
        values = r.json()['https://uri.fiware.org/ns/data-models#temperature'][0]['values']
        temperature = []
        for item in values:
            temperature.append(round(item[0], 3))
        if len(temperature) == 4:
            temperature.pop()

        # We need 3 months predictions. Given our model, that means we need to run the
        # model three times, each time building a sequence of three values (as required by
        # our model) -> First and second predictions will be used to predict third month           
        # We create a sequence of dimension 2 with three values [precipitation, temperature]
        # as for instance:
        # [[120, 12],
        #  [18, 14],
        #  [0, 15]]
        input_sequence = []
        for items in zip(precipitation, temperature):
            input_sequence.append([items[0], items[1]])
        logger.info('input_sequence: %s', input_sequence)

        ### CALLING BENTOML /predict HERE ###
        # Calling the inferance three times while re-building the 
        # sequence at every iteration using the last prediction
        # 1. get the inference API (behind /predict)
        # 2. build a request object from the input data
        # 3. perform the prediction
        logger.info('Calling bentoml /predict ...')
        predictions = []
        for _ in range(3):
            predict_api = self.bento_service.inference_apis[0]
            predict_req = Request.from_values(data=str(input_sequence))
            predict_res = predict_api.handle_request(predict_req)
            prediction = predict_res.get_json()
            logger.info('Prediction received from /predict: %s', prediction)
            predictions.append(prediction)
            input_sequence.pop(0)
            input_sequence.append(prediction)
        
        logger.info('final input sequence: %s', input_sequence)

        # We store this prevision as CSV for use by the hydrological model
        logger.info('saving sequence to csv')
        with open('csvfile.csv', 'w', newline='') as f:
            write = csv.writer(f)
            write.writerows(input_sequence)

        with open('csvfile.csv') as f:
            reader = csv.reader(f)
            sequence_read = list(reader)
        logger.info('Input sequence read from file: %s', sequence_read)


        # # prediction is a list of 2 elements, [precipitation, temperature]
        # # we update the target entity by creating a fragment and a
        # # POST entities/{entityId}/attrs/)
        # precipitation_pred, temperature_pred = prediction

        # # Get the current time and use it for time of prediction
        # timezone_GMT = pytz.timezone('GMT')
        # predictedAt = timezone_GMT.localize(datetime.now().replace(microsecond=0)).isoformat()

        # # Build a fragment to update 
        # fragment = {
        #     "precipitationPredicted": {
        #         "type": "Property",
        #         "datasetId": "urn:ngsi-ld:Dataset:flow:0:30:day",
        #         "computedBy": {
        #             "type": "Relationship",
        #             "object": "urn:ngsi-ld:MLModel:precipitationTemperature:predict"
        #         },
        #         "value": round(precipitation_pred, 1),
        #         "observedAt": predictedAt,
        #         "unitCode": "MM"
        #     },
        #     "temperaturePredicted": {
        #         "type": "Property",
        #         "datasetId": "urn:ngsi-ld:Dataset:flow:0:30:day",
        #         "computedBy": {
        #             "type": "Relationship",
        #             "object": "urn:ngsi-ld:MLModel:precipitationTemperature:predict"
        #         },
        #         "value": round(temperature_pred, 1),
        #         "observedAt": predictedAt,
        #         "timeInterval": {
        #             "type": "Property",
        #             "value": 30,
        #             "unitCode": "DAY"
        #         },
        #         "unitCode": "CEL"
        #     },
        #     '@context': AT_CONTEXT
        # }

        # URL_PATCH_PREDICTION = URL_ENTITIES + TARGET_ENTITY + '/attrs/'
        # r = requests.post(URL_PATCH_PREDICTION, json=fragment, headers=headers)
        # logger.info('requests status_code for (PATCH) Entity with prediction: %s', r.status_code)

        # Finally, respond to the initial received request (notification)
        # with empty 200
        response = make_response(
            str(sequence_read),
            200,
        )
        logger.info("-- Bye by from handle_ml_predict ...")
        return response


    def setup_bento_service_api_routes(self):
        """
        Setup a route for each InferenceAPI object defined in bento_service
        """
        for api in self.bento_service.inference_apis:
            route_function = self.bento_service_api_func_wrapper(api)
            self.app.add_url_rule(
                rule="/{}".format(api.route),
                endpoint=api.name,
                view_func=route_function,
                methods=api.input_adapter.HTTP_METHODS,
            )

    def bento_service_api_func_wrapper(self, api: InferenceAPI):
        """
        Create api function for flask route, it wraps around user defined API
        callback and adapter class, and adds request logging and instrument metrics
        """

        def api_func():
            # handle_request may raise 4xx or 5xx exception.
            try:
                if request.headers.get(self.request_header_flag):
                    reqs = DataLoader.split_requests(request.get_data())
                    responses = api.handle_batch_request(reqs)
                    response_body = DataLoader.merge_responses(responses)
                    response = make_response(response_body)
                else:
                    response = api.handle_request(request)
            except BentoMLException as e:
                log_exception(sys.exc_info())

                if 400 <= e.status_code < 500 and e.status_code not in (401, 403):
                    response = make_response(
                        jsonify(
                            message="BentoService error handling API request: %s"
                            % str(e)
                        ),
                        e.status_code,
                    )
                else:
                    response = make_response('', e.status_code)
            except Exception:  # pylint: disable=broad-except
                # For all unexpected error, return 500 by default. For example,
                # if users' model raises an error of division by zero.
                log_exception(sys.exc_info())

                response = make_response(
                    'An error has occurred in BentoML user code when handling this '
                    'request, find the error details in server logs',
                    500,
                )

            return response

        def api_func_with_tracing():
            with get_tracer().span(
                service_name=f"BentoService.{self.bento_service.name}",
                span_name=f"InferenceAPI {api.name} HTTP route",
                request_headers=request.headers,
            ):
                return api_func()

        return api_func_with_tracing
