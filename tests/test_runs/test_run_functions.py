import arff
import json
import random
import time
import sys

import numpy as np

import openml
import openml.exceptions
import openml._api_calls
import sklearn

from openml.testing import TestBase
from openml.runs.functions import _run_task_get_arffcontent, \
    _get_seeded_model, _run_exists, _extract_arfftrace, \
    _extract_arfftrace_attributes, _prediction_to_row, _check_n_jobs

from sklearn.naive_bayes import GaussianNB
from sklearn.model_selection._search import BaseSearchCV
from sklearn.tree import DecisionTreeClassifier, ExtraTreeClassifier
from sklearn.preprocessing.imputation import Imputer
from sklearn.dummy import DummyClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import VarianceThreshold
from sklearn.linear_model import LogisticRegression, SGDClassifier, \
    LinearRegression
from sklearn.ensemble import RandomForestClassifier, BaggingClassifier
from sklearn.svm import SVC
from sklearn.model_selection import RandomizedSearchCV, GridSearchCV, \
    StratifiedKFold
from sklearn.pipeline import Pipeline

if sys.version_info[0] >= 3:
    from unittest import mock
else:
    import mock


class TestRun(TestBase):

    def _wait_for_processed_run(self, run_id, max_waiting_time_seconds):
        # it can take a while for a run to be processed on the OpenML (test) server
        # however, sometimes it is good to wait (a bit) for this, to properly test
        # a function. In this case, we wait for max_waiting_time_seconds on this
        # to happen, probing the server every 10 seconds to speed up the process

        # time.time() works in seconds
        start_time = time.time()
        while time.time() - start_time < max_waiting_time_seconds:
            run = openml.runs.get_run(run_id)
            if len(run.evaluations) > 0:
                return
            else:
                time.sleep(10)

    def _check_serialized_optimized_run(self, run_id):
        run = openml.runs.get_run(run_id)
        task = openml.tasks.get_task(run.task_id)

        # TODO: assert holdout task

        # downloads the predictions of the old task
        predictions_url = openml._api_calls._file_id_to_url(run.output_files['predictions'])
        predictions = arff.loads(openml._api_calls._read_url(predictions_url))

        # downloads the best model based on the optimization trace
        # suboptimal (slow), and not guaranteed to work if evaluation
        # engine is behind. TODO: mock this? We have the arff already on the server
        self._wait_for_processed_run(run_id, 80)
        model_prime = openml.runs.initialize_model_from_trace(run_id, 0, 0)

        run_prime = openml.runs.run_task(task, model_prime, avoid_duplicate_runs=False)
        predictions_prime = run_prime._generate_arff_dict()

        self.assertEquals(len(predictions_prime['data']), len(predictions['data']))

        # The original search model does not submit confidence bounds,
        # so we can not compare the arff line
        compare_slice = [0, 1, 2, -1, -2]
        for idx in range(len(predictions['data'])):
            # depends on the assumption "predictions are in same order"
            # that does not necessarily hold.
            # But with the current code base, it holds.
            for col_idx in compare_slice:
                self.assertEquals(predictions['data'][idx][col_idx], predictions_prime['data'][idx][col_idx])

        return True


    def _perform_run(self, task_id, num_instances, clf, check_setup=True):
        task = openml.tasks.get_task(task_id)
        run = openml.runs.run_task(task, clf, openml.config.avoid_duplicate_runs)
        run_ = run.publish()
        self.assertEqual(run_, run)
        self.assertIsInstance(run.dataset_id, int)

        # check arff output
        self.assertEqual(len(run.data_content), num_instances)

        if check_setup:
            # test the initialize setup function
            run_id = run_.run_id
            run_server = openml.runs.get_run(run_id)
            clf_server = openml.setups.initialize_model(run_server.setup_id)

            flow_local = openml.flows.sklearn_to_flow(clf)
            flow_server = openml.flows.sklearn_to_flow(clf_server)

            openml.flows.assert_flows_equal(flow_local, flow_server)

            # and test the initialize setup from run function
            clf_server2 = openml.runs.initialize_model_from_run(run_server.run_id)
            flow_server2 = openml.flows.sklearn_to_flow(clf_server2)
            openml.flows.assert_flows_equal(flow_local, flow_server2)

            #self.assertEquals(clf.get_params(), clf_prime.get_params())
            # self.assertEquals(clf, clf_prime)

        downloaded = openml.runs.get_run(run_.run_id)
        assert('openml-python' in downloaded.tags)

        return run

    def test_run_regression_on_classif_task(self):
        task_id = 115

        clf = LinearRegression()
        task = openml.tasks.get_task(task_id)
        self.assertRaises(AttributeError, openml.runs.run_task,
                          task=task, model=clf, avoid_duplicate_runs=False)

    @mock.patch('openml.flows.sklearn_to_flow')
    def test_check_erronous_sklearn_flow_fails(self, sklearn_to_flow_mock):
        task_id = 115
        task = openml.tasks.get_task(task_id)

        # Invalid parameter values
        clf = LogisticRegression(C='abc')
        self.assertEqual(sklearn_to_flow_mock.call_count, 0)
        self.assertRaisesRegexp(ValueError, "Penalty term must be positive; got \(C='abc'\)",
                                openml.runs.run_task, task=task, model=clf)

    def test_run_and_upload(self):
        # This unit test is ment to test the following functions, using a varity of flows:
        # - openml.runs.run_task()
        # - openml.runs.OpenMLRun.publish()
        # - openml.runs.initialize_model()
        # - [implicitly] openml.setups.initialize_model()
        # - openml.runs.initialize_model_from_trace()
        task_id = 119 # diabates dataset
        num_test_instances = 253 # 33% holdout task
        num_folds = 1 # because of holdout
        num_iterations = 5 # for base search classifiers

        clfs = [LogisticRegression(),
                Pipeline(steps=(('scaler', StandardScaler(with_mean=False)),
                                ('dummy', DummyClassifier(strategy='prior')))),
                Pipeline(steps=[('Imputer', Imputer(strategy='median')),
                                ('VarianceThreshold', VarianceThreshold()),
                                ('Estimator', RandomizedSearchCV(DecisionTreeClassifier(),
                                                                 {'min_samples_split': [2 ** x for x in
                                                                                        range(1, 7 + 1)],
                                                                  'min_samples_leaf': [2 ** x for x in
                                                                                       range(0, 6 + 1)]},
                                                                 cv=3, n_iter=10))]),
                GridSearchCV(BaggingClassifier(base_estimator=SVC()),
                             {"base_estimator__C": [0.01, 0.1, 10],
                              "base_estimator__gamma": [0.01, 0.1, 10]}),
                RandomizedSearchCV(RandomForestClassifier(n_estimators=5),
                                   {"max_depth": [3, None],
                                    "max_features": [1, 2, 3, 4],
                                    "min_samples_split": [2, 3, 4, 5, 6, 7, 8, 9, 10],
                                    "min_samples_leaf": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                                    "bootstrap": [True, False],
                                    "criterion": ["gini", "entropy"]},
                                    cv=StratifiedKFold(n_splits=2),
                                    n_iter=num_iterations)
        ]

        for clf in clfs:
            run = self._perform_run(task_id, num_test_instances, clf)
            if isinstance(clf, BaseSearchCV):
                if isinstance(clf, GridSearchCV):
                    grid_iterations = 1
                    for param in clf.param_grid:
                        grid_iterations *= len(clf.param_grid[param])
                    self.assertEqual(len(run.trace_content), grid_iterations * num_folds)
                else:
                    self.assertEqual(len(run.trace_content), num_iterations * num_folds)
                check_res = self._check_serialized_optimized_run(run.run_id)
                self.assertTrue(check_res)

            # todo: check if runtime is present
            pass


    def test_initialize_model_from_run(self):
        clf = sklearn.pipeline.Pipeline(steps=[('Imputer', Imputer(strategy='median')),
                                               ('VarianceThreshold', VarianceThreshold(threshold=0.05)),
                                               ('Estimator', GaussianNB())])
        task = openml.tasks.get_task(11)
        run = openml.runs.run_task(task, clf, avoid_duplicate_runs=False)
        run_ = run.publish()
        run = openml.runs.get_run(run_.run_id)

        modelR = openml.runs.initialize_model_from_run(run.run_id)
        modelS = openml.setups.initialize_model(run.setup_id)

        flowR = openml.flows.sklearn_to_flow(modelR)
        flowS = openml.flows.sklearn_to_flow(modelS)
        flowL = openml.flows.sklearn_to_flow(clf)
        openml.flows.assert_flows_equal(flowR, flowL)
        openml.flows.assert_flows_equal(flowS, flowL)

        self.assertEquals(flowS.components['Imputer'].parameters['strategy'], '"median"')
        self.assertEquals(flowS.components['VarianceThreshold'].parameters['threshold'], '0.05')
        pass

    def test_get_run_trace(self):
        # get_run_trace is already tested implicitly in test_run_and_publish
        # this test is a bit additional.
        num_iterations = 10
        num_folds = 1
        task_id = 119
        run_id = None

        task = openml.tasks.get_task(task_id)
        # IMPORTANT! Do not sentinel this flow. is faster if we don't wait on openml server
        clf = RandomizedSearchCV(RandomForestClassifier(random_state=42),
                                 {"max_depth": [3, None],
                                  "max_features": [1, 2, 3, 4],
                                  "bootstrap": [True, False],
                                  "criterion": ["gini", "entropy"]},
                                 num_iterations, random_state=42)

        # [SPEED] make unit test faster by exploiting run information from the past
        try:
            # in case the run did not exists yet
            run = openml.runs.run_task(task, clf, avoid_duplicate_runs=True)
            run = run.publish()
            self._wait_for_processed_run(run.run_id, 80)
            run_id = run.run_id
        except openml.exceptions.PyOpenMLError:
            # run was already
            flow = openml.flows.sklearn_to_flow(clf)
            flow_exists = openml.flows.flow_exists(flow.name, flow.external_version)
            self.assertIsInstance(flow_exists, int)
            downloaded_flow = openml.flows.get_flow(flow_exists)
            setup_exists = openml.setups.setup_exists(downloaded_flow, clf)
            self.assertIsInstance(setup_exists, int)
            run_ids = _run_exists(task.task_id, setup_exists)
            run_id = random.choice(list(run_ids))

        # now the actual unit test ...
        run_trace = openml.runs.get_run_trace(run_id)
        self.assertEqual(len(run_trace.trace_iterations), num_iterations * num_folds)

    def test__run_exists(self):
        # would be better to not sentinel these clfs ..
        clfs = [sklearn.pipeline.Pipeline(steps=[('Imputer', Imputer(strategy='mean')),
                                                ('VarianceThreshold', VarianceThreshold(threshold=0.05)),
                                                ('Estimator', GaussianNB())]),
                sklearn.pipeline.Pipeline(steps=[('Imputer', Imputer(strategy='most_frequent')),
                                                 ('VarianceThreshold', VarianceThreshold(threshold=0.1)),
                                                 ('Estimator', DecisionTreeClassifier(max_depth=4))])]
        task = openml.tasks.get_task(1)

        for clf in clfs:
            try:
                # first populate the server with this run.
                # skip run if it was already performed.
                run = openml.runs.run_task(task, clf, avoid_duplicate_runs=True)
                run.publish()
            except openml.exceptions.PyOpenMLError:
                # run already existed. Great.
                pass

            flow = openml.flows.sklearn_to_flow(clf)
            flow_exists = openml.flows.flow_exists(flow.name, flow.external_version)
            self.assertIsInstance(flow_exists, int)
            downloaded_flow = openml.flows.get_flow(flow_exists)
            setup_exists = openml.setups.setup_exists(downloaded_flow, clf)
            self.assertIsInstance(setup_exists, int)
            run_ids = _run_exists(task.task_id, setup_exists)
            self.assertGreater(len(run_ids), 0)


    def test__get_seeded_model(self):
        # randomized models that are initialized without seeds, can be seeded
        randomized_clfs = [
            BaggingClassifier(),
            RandomizedSearchCV(RandomForestClassifier(),
                               {"max_depth": [3, None],
                                "max_features": [1, 2, 3, 4],
                                "bootstrap": [True, False],
                                "criterion": ["gini", "entropy"],
                                "random_state" : [-1, 0, 1, 2]},
                               ),
            DummyClassifier()
        ]

        for clf in randomized_clfs:
            const_probe = 42
            all_params = clf.get_params()
            params = [key for key in all_params if key.endswith('random_state')]
            self.assertGreater(len(params), 0)

            # before param value is None
            for param in params:
                self.assertIsNone(all_params[param])

            # now seed the params
            clf_seeded = _get_seeded_model(clf, const_probe)
            new_params = clf_seeded.get_params()

            randstate_params = [key for key in new_params if key.endswith('random_state')]

            # afterwards, param value is set
            for param in randstate_params:
                self.assertIsInstance(new_params[param], int)
                self.assertIsNotNone(new_params[param])

    def test__get_seeded_model_raises(self):
        # the _get_seeded_model should raise exception if random_state is anything else than an int
        randomized_clfs = [
            BaggingClassifier(random_state=np.random.RandomState(42)),
            DummyClassifier(random_state="OpenMLIsGreat")
        ]

        for clf in randomized_clfs:
            self.assertRaises(ValueError, _get_seeded_model, model=clf, seed=42)

    def test__extract_arfftrace(self):
        param_grid = {"max_depth": [3, None],
                      "max_features": [1, 2, 3, 4],
                      "bootstrap": [True, False],
                      "criterion": ["gini", "entropy"]}
        num_iters = 10
        task = openml.tasks.get_task(20)
        clf = RandomizedSearchCV(RandomForestClassifier(), param_grid, num_iters)
        # just run the task
        train, _ = task.get_train_test_split_indices(0, 0)
        X, y = task.get_X_and_y()
        clf.fit(X[train], y[train])

        trace_attribute_list = _extract_arfftrace_attributes(clf)
        trace_list = _extract_arfftrace(clf, 0, 0)
        self.assertIsInstance(trace_attribute_list, list)
        self.assertEquals(len(trace_attribute_list), 5 + len(param_grid))
        self.assertIsInstance(trace_list, list)
        self.assertEquals(len(trace_list), num_iters)

        # found parameters
        optimized_params = set()

        for att_idx in range(len(trace_attribute_list)):
            att_type = trace_attribute_list[att_idx][1]
            att_name = trace_attribute_list[att_idx][0]
            if att_name.startswith("parameter_"):
                # add this to the found parameters
                param_name = att_name[len("parameter_"):]
                optimized_params.add(param_name)

                for line_idx in range(len(trace_list)):
                    val = json.loads(trace_list[line_idx][att_idx])
                    legal_values = param_grid[param_name]
                    self.assertIn(val, legal_values)
            else:
                # repeat, fold, itt, bool
                for line_idx in range(len(trace_list)):
                    val = trace_list[line_idx][att_idx]
                    if isinstance(att_type, list):
                        self.assertIn(val, att_type)
                    elif att_name in ['repeat', 'fold', 'iteration']:
                        self.assertIsInstance(trace_list[line_idx][att_idx], int)
                    else: # att_type = real
                        self.assertIsInstance(trace_list[line_idx][att_idx], float)


        self.assertEqual(set(param_grid.keys()), optimized_params)

    def test__prediction_to_row(self):
        repeat_nr = 0
        fold_nr = 0
        clf = sklearn.pipeline.Pipeline(steps=[('Imputer', Imputer(strategy='mean')),
                                               ('VarianceThreshold', VarianceThreshold(threshold=0.05)),
                                               ('Estimator', GaussianNB())])
        task = openml.tasks.get_task(20)
        train, test = task.get_train_test_split_indices(repeat_nr, fold_nr)
        X, y = task.get_X_and_y()
        clf.fit(X[train], y[train])

        test_X = X[test]
        test_y = y[test]

        probaY = clf.predict_proba(test_X)
        predY = clf.predict(test_X)
        for idx in range(0, len(test_X)):
            arff_line = _prediction_to_row(repeat_nr, fold_nr, idx,
                                           task.class_labels[test_y[idx]],
                                           predY[idx], probaY[idx], task.class_labels, clf.classes_)

            self.assertIsInstance(arff_line, list)
            self.assertEqual(len(arff_line), 5 + len(task.class_labels))
            self.assertEqual(arff_line[0], repeat_nr)
            self.assertEqual(arff_line[1], fold_nr)
            self.assertEqual(arff_line[2], idx)
            sum = 0.0
            for att_idx in range(3, 3 + len(task.class_labels)):
                self.assertIsInstance(arff_line[att_idx], float)
                self.assertGreaterEqual(arff_line[att_idx], 0.0)
                self.assertLessEqual(arff_line[att_idx], 1.0)
                sum += arff_line[att_idx]
            self.assertAlmostEqual(sum, 1.0)

            self.assertIn(arff_line[-1], task.class_labels)
            self.assertIn(arff_line[-2], task.class_labels)
        pass
    

    def test_run_with_classifiers_in_param_grid(self):
        task = openml.tasks.get_task(115)

        param_grid = {
            "base_estimator": [DecisionTreeClassifier(), ExtraTreeClassifier()]
        }

        clf = GridSearchCV(BaggingClassifier(), param_grid=param_grid)
        self.assertRaises(TypeError, openml.runs.run_task,
                          task=task, model=clf, avoid_duplicate_runs=False)

    def test__run_task_get_arffcontent(self):
        timing_measures = {'usercpu_time_millis_testing', 'usercpu_time_millis_training', 'usercpu_time_millis'}
        task = openml.tasks.get_task(7)
        class_labels = task.class_labels
        num_instances = 3196
        num_folds = 10
        num_repeats = 1

        clf = SGDClassifier(loss='hinge', random_state=1)
        self.assertRaisesRegexp(AttributeError,
                                "probability estimates are not available for loss='hinge'",
                                openml.runs.functions._run_task_get_arffcontent,
                                clf, task, class_labels)

        clf = SGDClassifier(loss='log', random_state=1)
        res = openml.runs.functions._run_task_get_arffcontent(clf, task, class_labels)
        arff_datacontent, arff_tracecontent, _, detailed_evaluations = res
        # predictions
        self.assertIsInstance(arff_datacontent, list)
        # trace. SGD does not produce any
        self.assertIsInstance(arff_tracecontent, type(None))

        self.assertIsInstance(detailed_evaluations, dict)
        if sys.version_info[:2] >= (3, 3): # check_n_jobs follows from the used clf:
            self.assertEquals(set(detailed_evaluations.keys()), timing_measures)
            for measure in timing_measures:
                num_rep_entrees = len(detailed_evaluations[measure])
                self.assertEquals(num_rep_entrees, num_repeats)
                for rep in range(num_rep_entrees):
                    num_fold_entrees = len(detailed_evaluations[measure][rep])
                    self.assertEquals(num_fold_entrees, num_folds)
                    for fold in range(num_fold_entrees):
                        evaluation = detailed_evaluations[measure][rep][fold]
                        self.assertIsInstance(evaluation, float)
                        self.assertGreater(evaluation, 0) # should take at least one millisecond (?)
                        self.assertLess(evaluation, 60) # pessimistic


        # 10 times 10 fold CV of 150 samples
        self.assertEqual(len(arff_datacontent), num_instances * num_repeats)
        for arff_line in arff_datacontent:
            # check number columns
            self.assertEqual(len(arff_line), 7)
            # check repeat
            self.assertGreaterEqual(arff_line[0], 0)
            self.assertLessEqual(arff_line[0], num_repeats - 1)
            # check fold
            self.assertGreaterEqual(arff_line[1], 0)
            self.assertLessEqual(arff_line[1], num_folds - 1)
            # check row id
            self.assertGreaterEqual(arff_line[2], 0)
            self.assertLessEqual(arff_line[2], num_instances - 1)
            # check confidences
            self.assertAlmostEqual(sum(arff_line[3:5]), 1.0)
            self.assertIn(arff_line[5], ['won', 'nowin'])
            self.assertIn(arff_line[6], ['won', 'nowin'])

    def test_get_run(self):
        # this run is not available on test
        openml.config.server = self.production_server
        run = openml.runs.get_run(473350)
        self.assertEqual(run.dataset_id, 1167)
        self.assertEqual(run.evaluations['f_measure'], 0.624668)
        for i, value in [(0, 0.66233),
                         (1, 0.639286),
                         (2, 0.567143),
                         (3, 0.745833),
                         (4, 0.599638),
                         (5, 0.588801),
                         (6, 0.527976),
                         (7, 0.666365),
                         (8, 0.56759),
                         (9, 0.64621)]:
            self.assertEqual(run.detailed_evaluations['f_measure'][0][i], value)
        assert('weka' in run.tags)
        assert('stacking' in run.tags)

    def _check_run(self, run):
        self.assertIsInstance(run, dict)
        self.assertEqual(len(run), 5)

    def test_get_runs_list(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        runs = openml.runs.list_runs(id=[2])
        self.assertEqual(len(runs), 1)
        for rid in runs:
            self._check_run(runs[rid])

    def test_get_runs_list_by_task(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        task_ids = [20]
        runs = openml.runs.list_runs(task=task_ids)
        self.assertGreaterEqual(len(runs), 590)
        for rid in runs:
            self.assertIn(runs[rid]['task_id'], task_ids)
            self._check_run(runs[rid])
        num_runs = len(runs)

        task_ids.append(21)
        runs = openml.runs.list_runs(task=task_ids)
        self.assertGreaterEqual(len(runs), num_runs + 1)
        for rid in runs:
            self.assertIn(runs[rid]['task_id'], task_ids)
            self._check_run(runs[rid])

    def test_get_runs_list_by_uploader(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        # 29 is Dominik Kirchhoff - Joaquin and Jan have too many runs right now
        uploader_ids = [29]

        runs = openml.runs.list_runs(uploader=uploader_ids)
        self.assertGreaterEqual(len(runs), 2)
        for rid in runs:
            self.assertIn(runs[rid]['uploader'], uploader_ids)
            self._check_run(runs[rid])
        num_runs = len(runs)

        uploader_ids.append(274)

        runs = openml.runs.list_runs(uploader=uploader_ids)
        self.assertGreaterEqual(len(runs), num_runs + 1)
        for rid in runs:
            self.assertIn(runs[rid]['uploader'], uploader_ids)
            self._check_run(runs[rid])

    def test_get_runs_list_by_flow(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        flow_ids = [1154]
        runs = openml.runs.list_runs(flow=flow_ids)
        self.assertGreaterEqual(len(runs), 1)
        for rid in runs:
            self.assertIn(runs[rid]['flow_id'], flow_ids)
            self._check_run(runs[rid])
        num_runs = len(runs)

        flow_ids.append(1069)
        runs = openml.runs.list_runs(flow=flow_ids)
        self.assertGreaterEqual(len(runs), num_runs + 1)
        for rid in runs:
            self.assertIn(runs[rid]['flow_id'], flow_ids)
            self._check_run(runs[rid])

    def test_get_runs_pagination(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        uploader_ids = [1]
        size = 10
        max = 100
        for i in range(0, max, size):
            runs = openml.runs.list_runs(offset=i, size=size, uploader=uploader_ids)
            self.assertGreaterEqual(size, len(runs))
            for rid in runs:
                self.assertIn(runs[rid]["uploader"], uploader_ids)

    def test_get_runs_list_by_filters(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        ids = [505212, 6100]
        tasks = [2974, 339]
        uploaders_1 = [1, 2]
        uploaders_2 = [29, 274]
        flows = [74, 1718]

        self.assertRaises(openml.exceptions.OpenMLServerError, openml.runs.list_runs)

        runs = openml.runs.list_runs(id=ids)
        self.assertEqual(len(runs), 2)

        runs = openml.runs.list_runs(task=tasks)
        self.assertGreaterEqual(len(runs), 2)

        runs = openml.runs.list_runs(uploader=uploaders_2)
        self.assertGreaterEqual(len(runs), 10)

        runs = openml.runs.list_runs(flow=flows)
        self.assertGreaterEqual(len(runs), 100)

        runs = openml.runs.list_runs(id=ids, task=tasks, uploader=uploaders_1)

    def test_get_runs_list_by_tag(self):
        # TODO: comes from live, no such lists on test
        openml.config.server = self.production_server
        runs = openml.runs.list_runs(tag='curves')
        self.assertGreaterEqual(len(runs), 1)

    def test_run_on_dataset_with_missing_labels(self):
        # Check that _run_task_get_arffcontent works when one of the class
        # labels only declared in the arff file, but is not present in the
        # actual data

        task = openml.tasks.get_task(2)
        class_labels = task.class_labels

        model = Pipeline(steps=[('Imputer', Imputer(strategy='median')),
                                ('Estimator', DecisionTreeClassifier())])

        data_content, _, _, _ = _run_task_get_arffcontent(model, task, class_labels)
        # 2 folds, 5 repeats; keep in mind that this task comes from the test
        # server, the task on the live server is different
        self.assertEqual(len(data_content), 4490)
        for row in data_content:
            # repeat, fold, row_id, 6 confidences, prediction and correct label
            self.assertEqual(len(row), 11)

