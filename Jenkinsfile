pipeline {
    agent any

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timestamps()
    }

    parameters {
        string(name: 'GITHUB_REF', defaultValue: '', description: 'Reviewed GitHub branch ref, for example refs/heads/feat/x')
        string(name: 'GITHUB_COMMIT', defaultValue: '', description: 'Reviewed full lowercase 40-character GitHub SHA')
    }

    environment {
        GIT_REPO_URL = 'git@github.com:lsq1030757028/tapd-capability.git'
        PYTHON_BUILD_IMAGE = 'python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
        PIPELINE_CONTRACT = 'tapd-capability-coding-v1-fixed-sha-build-no-deploy'
    }

    stages {
        stage('Validate fixed GitHub source') {
            steps {
                deleteDir()
                script {
                    def githubRef = params.GITHUB_REF ?: ''
                    def githubCommit = params.GITHUB_COMMIT ?: ''
                    def credentialId = env.CODING_GITHUB_SSH_CREDENTIAL_ID ?: ''

                    if (githubRef != githubRef.trim()) {
                        error('GITHUB_REF must not contain surrounding whitespace')
                    }
                    if (!(githubRef ==~ /^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._\/-]*$/) ||
                        githubRef.contains('..') || githubRef.contains('//') ||
                        githubRef.contains('/.') || githubRef.contains('.lock/') ||
                        githubRef.contains('@{') || githubRef.endsWith('/') ||
                        githubRef.endsWith('.') || githubRef.endsWith('.lock')) {
                        error('GITHUB_REF must be a safe refs/heads branch ref')
                    }
                    if (!(githubCommit ==~ /^[0-9a-f]{40}$/)) {
                        error('GITHUB_COMMIT must be a full lowercase 40-character SHA')
                    }
                    if (credentialId != credentialId.trim() ||
                        !(credentialId ==~ /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/)) {
                        error('CODING_GITHUB_SSH_CREDENTIAL_ID is required and must name a controlled job credential')
                    }

                    env.SOURCE_REF = githubRef
                    env.SOURCE_REVISION = githubCommit
                    env.GITHUB_CHECKOUT_CREDENTIAL_ID = credentialId
                }
            }
        }

        stage('Checkout exact private GitHub SHA') {
            steps {
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: env.SOURCE_REVISION]],
                    userRemoteConfigs: [[
                        url: env.GIT_REPO_URL,
                        credentialsId: env.GITHUB_CHECKOUT_CREDENTIAL_ID,
                        refspec: "+${env.SOURCE_REF}:refs/remotes/origin/reviewed"
                    ]],
                    extensions: [
                        [$class: 'CloneOption', noTags: true, shallow: false, honorRefspec: true],
                        [$class: 'CleanBeforeCheckout']
                    ]
                ])
                sh '''
                    set -eu
                    echo "pipeline_contract=$PIPELINE_CONTRACT"
                    test "$SOURCE_REVISION" = "$GITHUB_COMMIT"
                    test "$(git rev-parse HEAD)" = "$GITHUB_COMMIT"
                    test "$(git remote get-url origin)" = "$GIT_REPO_URL"
                    git merge-base --is-ancestor "$GITHUB_COMMIT" refs/remotes/origin/reviewed
                    test -z "$(git status --porcelain)"
                '''
            }
        }

        stage('Pinned MCP regression and Docker test image') {
            steps {
                sh '''
                    set -eu
                    mkdir -p ci-artifacts
                    test_tag="tapd-capability-ci-test:${GITHUB_COMMIT}"
                    docker build --pull --target test \
                        --iidfile ci-artifacts/test-image.iid \
                        --tag "$test_tag" .
                    docker run --rm \
                        --volume "$PWD:/workspace" \
                        --workdir /workspace \
                        "$PYTHON_BUILD_IMAGE" \
                        sh -ec 'python -m pip install --quiet --requirement adapters/requirements.txt pytest==8.4.1; python -m pytest tests/ -q --ignore=tests/acceptance_live.py --junitxml=ci-artifacts/mcp-full-suite.xml'
                '''
            }
        }

        stage('Docker runtime candidate and identity metadata') {
            steps {
                sh '''
                    set -eu
                    runtime_tag="tapd-capability-ci-runtime:${GITHUB_COMMIT}"
                    docker build --pull --target runtime \
                        --build-arg "SOURCE_REVISION=$GITHUB_COMMIT" \
                        --iidfile ci-artifacts/runtime-image.iid \
                        --tag "$runtime_tag" .
                    runtime_label="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$runtime_tag")"
                    test "$runtime_label" = "$GITHUB_COMMIT"
                    python3 ci/write_image_metadata.py \
                        --output ci-artifacts/image-metadata.json \
                        --repository "$GIT_REPO_URL" \
                        --source-ref "$GITHUB_REF" \
                        --source-revision "$GITHUB_COMMIT" \
                        --test-image-id "$(tr -d '\r\n' < ci-artifacts/test-image.iid)" \
                        --runtime-image-id "$(tr -d '\r\n' < ci-artifacts/runtime-image.iid)" \
                        --runtime-label-revision "$runtime_label"
                '''
            }
        }
    }

    post {
        always {
            junit testResults: 'ci-artifacts/*.xml', allowEmptyResults: true
            archiveArtifacts artifacts: 'ci-artifacts/*', allowEmptyArchive: true, fingerprint: true
        }
        success {
            echo 'Build-only fixed-SHA candidate complete; no registry push and no deployment were performed.'
        }
    }
}
