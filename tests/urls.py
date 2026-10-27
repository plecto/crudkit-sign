from django.urls import include, path

urlpatterns = [
    path("sign/", include("crudkit_sign.urls")),
]
