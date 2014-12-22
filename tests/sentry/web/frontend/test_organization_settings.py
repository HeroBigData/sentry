from __future__ import absolute_import

from django.core.urlresolvers import reverse

from sentry.models import Organization
from sentry.testutils import TestCase, PermissionTestCase


class OrganizationSettingsPermissionTest(PermissionTestCase):
    def setUp(self):
        super(OrganizationSettingsPermissionTest, self).setUp()
        self.path = reverse('sentry-organization-settings', args=[self.organization.slug])

    def test_teamless_admin_cannot_load(self):
        self.assert_teamless_admin_cannot_access(self.path)

    def test_org_admin_can_load(self):
        self.assert_org_admin_can_access(self.path)

    def test_org_member_cannot_load(self):
        self.assert_org_member_cannot_access(self.path)


class OrganizationSettingsTest(TestCase):
    def test_renders_with_context(self):
        organization = self.create_organization(name='foo', owner=self.user)
        team = self.create_team(organization=organization)
        project = self.create_project(team=team)

        path = reverse('sentry-organization-settings', args=[organization.slug])

        self.login_as(self.user)

        resp = self.client.get(path)

        assert resp.status_code == 200

        self.assertTemplateUsed(resp, 'sentry/organization-settings.html')

        assert resp.context['organization'] == organization
        assert resp.context['form']

    def test_saves(self):
        organization = self.create_organization(name='foo', owner=self.user)
        team = self.create_team(organization=organization)
        project = self.create_project(team=team)

        path = reverse('sentry-organization-settings', args=[organization.slug])

        self.login_as(self.user)

        resp = self.client.post(path, {'name': 'bar', 'slug': 'bar'})

        assert resp.status_code == 302

        organization = Organization.objects.get(id=organization.id)

        assert organization.name == 'bar'
        assert organization.slug == 'bar'
