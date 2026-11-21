"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from farmacia.admin import farmacia_admin_site
from farmacia.views import IniciarSesionView

urlpatterns = [
    path('', IniciarSesionView.as_view(), name='login'),
    path(
        'accounts/login/',
        RedirectView.as_view(
            pattern_name='login',
            permanent=False,
            query_string=True,
        ),
        name='login_legacy',
    ),
    path(
        'accounts/logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),
    path('farmacia/', include('farmacia.urls')),
    path('admin/', farmacia_admin_site.urls),
]

handler404 = "config.error_handlers.handler404"
handler500 = "config.error_handlers.handler500"
