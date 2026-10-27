from django.urls import path

from crudkit_sign import views

app_name = "crudkit_sign"

urlpatterns = [
    path("<str:token>/", views.sign_page, name="sign"),
    path("<str:token>/document.pdf", views.document, name="document"),
    path("<str:token>/submit/", views.submit, name="submit"),
    path("<str:token>/decline/", views.decline, name="decline"),
    path("<str:token>/done/", views.done, name="done"),
    path("<str:token>/signed.pdf", views.signed_document, name="signed"),
]
