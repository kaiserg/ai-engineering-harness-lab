// Controlador descartable dedicado a M7f. Ejecutar con el plugin matrix-auth.
import hudson.model.*
import hudson.security.*
import hudson.tasks.*
import jenkins.model.Jenkins
import jenkins.security.ApiTokenProperty
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermissions

def j = Jenkins.get()
if (j.getItem('atlas-tests') != null) return

def realm = new HudsonPrivateSecurityRealm(false)
j.setSecurityRealm(realm)
realm.createAccount('lab-admin', UUID.randomUUID().toString())
def reader = realm.createAccount('lab-reader', UUID.randomUUID().toString())
def access = new ProjectMatrixAuthorizationStrategy()
access.add(Jenkins.ADMINISTER, 'lab-admin')
access.add(Jenkins.READ, 'lab-reader')
j.setAuthorizationStrategy(access)
j.setNumExecutors(1)

def job = j.createProject(FreeStyleProject, 'atlas-tests')
def jobAccess = new AuthorizationMatrixProperty()
jobAccess.add(Item.READ, 'lab-reader')
job.addProperty(jobAccess)
job.addProperty(new ParametersDefinitionProperty(new BooleanParameterDefinition('STALE', false, 'Ejecutar la rama base')))
job.getBuildersList().add(new Shell('''#!/bin/sh
set -eu
rm -f harness-revision.json
rm -rf checkout
git init -q checkout
ref=refs/pull/1/head
if [ "$STALE" = true ]; then ref=refs/heads/master; fi
git -C checkout fetch --no-tags --depth=1 https://github.com/octocat/Hello-World.git "$ref"
git -C checkout checkout --detach FETCH_HEAD
sha=$(git -C checkout rev-parse HEAD)
printf '{"repository":"octocat/hello-world","pull_number":1,"revision":"%s"}\n' "$sha" > harness-revision.json
test -s checkout/README
'''))
job.getPublishersList().add(new ArtifactArchiver('harness-revision.json'))
job.save()
j.save()

def property = reader.getProperty(ApiTokenProperty)
def token = property.tokenStore.generateNewToken('harness-read').plainValue
reader.save()
def path = j.rootDir.toPath().resolve('harness-reader-token')
Files.createFile(path, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString('rw-------')))
Files.writeString(path, token)

for (stale in [false, true]) {
    job.scheduleBuild2(0, new Cause.UserIdCause(), new ParametersAction(new BooleanParameterValue('STALE', stale)))
}
