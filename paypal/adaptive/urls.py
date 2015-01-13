from django.conf.urls import *
from django.views.decorators.csrf import csrf_exempt

from paypal.adaptive import views


urlpatterns = patterns('',
    url(r'^redirect/', views.RedirectView.as_view(),
        name='paypal-redirect-adaptive'),
    url(r'^success/(?P<basket_id>\d+)/$', views.SuccessResponseView.as_view(),
        name='paypal-success-response-adaptive'),
    url(r'^cancel/(?P<basket_id>\d+)/$', views.CancelResponseView.as_view(),
        name='paypal-cancel-response-adaptive'),
    url(r'^shipping-options/(?P<basket_id>\d+)/', csrf_exempt(views.ShippingOptionsView.as_view()),
        name='paypal-shipping-options-adaptive'),
)
