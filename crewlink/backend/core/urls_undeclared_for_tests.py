"""URLconf used only by the default-deny tests: three deliberately bad routes."""
from django.http import HttpResponse
from django.urls import path
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class Forgot(APIView):  # forgot to declare permission_classes
    def get(self, request):
        return Response({"secret": True})


class Leaky(APIView):  # AllowAny on a route that is not an approved public route
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"secret": True})


def plain(request):  # plain Django view, outside DRF
    return HttpResponse("hi")


urlpatterns = [
    path("api/forgot/", Forgot.as_view()),
    path("api/leaky/", Leaky.as_view()),
    path("api/plain/", plain),
]
