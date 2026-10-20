// Gate every deployment on the pytest suite (no Spark needed), then deploy the bundle.
pipeline {
    agent { label 'python' }
    options { timestamps(); timeout(time: 30, unit: 'MINUTES') }

    environment {
        DATABRICKS_HOST  = credentials('databricks-host')
        DATABRICKS_TOKEN = credentials('databricks-token')
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install -q --upgrade pip
                    pip install -q -e ".[dev]"
                '''
            }
        }
        stage('Unit & contract tests') {
            steps {
                sh '''
                    . .venv/bin/activate
                    mkdir -p reports
                    pytest -m "not integration" --junitxml=reports/junit-unit.xml
                '''
            }
        }
        stage('Integration tests (mock Facets)') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pytest -m integration --junitxml=reports/junit-integration.xml
                '''
            }
        }
        stage('Bundle validate') {
            steps { sh 'databricks bundle validate -t qa' }
        }
        stage('Deploy to QA') {
            when { branch 'main' }
            steps { sh 'databricks bundle deploy -t qa' }
        }
        stage('Deploy to UAT') {
            when { branch 'release/*' }
            steps {
                input message: 'Deploy eligibility job to UAT?', ok: 'Deploy'
                sh 'databricks bundle deploy -t uat'
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: false, testResults: 'reports/junit-*.xml'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
    }
}
