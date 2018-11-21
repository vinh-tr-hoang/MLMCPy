import numpy as np
import timeit
from datetime import timedelta
import sys
import imp

from MLMCPy.input import Input
from MLMCPy.model import Model


class MLMCSimulator:
    """
    Computes an estimate based on the Multi-Level Monte Carlo algorithm.
    """
    def __init__(self, data, models):
        """
        Requires a data object that provides input samples and a list of models
        of increasing fidelity.

        :param data: Provides a data sampling function.
        :type data: Input
        :param models: Each model Produces outputs from sample data input.
        :type models: list(Model)
        """
        # Detect whether we have access to multiple cpus.
        self.__detect_parallelization()

        self.__check_init_parameters(data, models)

        self._data = data
        self._models = models
        self._num_levels = len(self._models)

        # Sample size to be taken at each level.
        self._sample_sizes = np.zeros(self._num_levels, dtype=np.int)

        # Used to compute sample sizes based on a fixed cost.
        self._target_cost = None

        # Sample size used in setup.
        self._initial_sample_size = 0

        # Desired level of precision.
        self._epsilons = np.zeros(1)

        # Cost of running model on a sample at each level.
        self._costs = np.zeros(1)

        # Number of elements in model output.
        self._output_size = 1

        # Enabled diagnostic text output.
        self._verbose = False

    def simulate(self, epsilon, initial_sample_size=1000, target_cost=None,
                 verbose=False):
        """
        Perform MLMC simulation.
        Computes number of samples per level before running simulations
        to determine estimates.

        :param epsilon: Desired accuracy to be achieved for each quantity of
            interest.
        :type epsilon: float, list of floats, or ndarray.
        :param initial_sample_size: Sample size used when computing sample sizes
            for each level in simulation.
        :type initial_sample_size: int
        :param target_cost: Target cost to run simulation.
        :type target_cost: float or int
        :param verbose: Whether to print useful diagnostic information.
        :type verbose: bool
        :return: Tuple of ndarrays
            (estimates, sample count per level, variances)
        """
        self._verbose = verbose and self._cpu_rank == 0

        self.__check_simulate_parameters(initial_sample_size, target_cost)

        if target_cost is not None:
            self._target_cost = float(target_cost)

        self._determine_output_size()

        # Compute optimal sample sizes for each level, as well as alpha value.
        self._setup_simulation(epsilon, initial_sample_size)

        # Run models and return estimate.
        return self._run_simulation()

    def _setup_simulation(self, epsilon, initial_sample_size):
        """
        Computes variance and cost at each level in order to estimate optimal
        number of samples at each level.

        :param epsilon: Epsilon values for each quantity of interest.
        :param initial_sample_size: Sample size used when computing sample sizes
            for each level in simulation.
        """
        self._initial_sample_size = initial_sample_size

        # Epsilon should be array that matches output width.
        self._epsilons = self._process_epsilon(epsilon)

        # Run models with initial sample sizes to compute costs, outputs.
        costs, variances = self._compute_costs_and_variances()

        # Compute optimal sample size at each level.
        self._compute_optimal_sample_sizes(costs, variances)

    def _run_simulation(self):
        """
        Compute estimate by extracting number of samples from each level
        determined in the setup phase.

        :return: tuple containing three ndarrays:
            estimates: Estimates for each quantity of interest
            sample_sizes: The sample sizes used at each level.
            variances: Variance of model outputs at each level.
        """
        # Restart sampling from beginning.
        self._data.reset_sampling()

        # Time simulation. If target_cost was specified we will need this
        # information later to approximate the target.
        start_time = timeit.default_timer()
        estimates, variances = self._run_simulation_loop()
        end_time = timeit.default_timer()

        if self._verbose:
            self._show_summary_data(estimates, variances, end_time - start_time)

        self._sample_sizes = self._sum_over_all_cpus(self._cpu_sample_sizes)

        return estimates, self._sample_sizes, variances

    def _run_simulation_loop(self):
        """
        Main simulation loop where sample sizes determined in setup phase are
        drawn from the input data and run through the models. Sums of the model
        outputs and their squares are accumulated in order to compute the
        final estimates and variances later.
        :return:
        """
        estimates = np.zeros(self._output_size)
        variances = np.zeros(self._output_size)

        for level in range(self._num_levels):

            if self._sample_sizes[level] == 0:

                # Even if we aren't taking any samples at this level,
                # it's possible that in an MPI environment the other processes
                # will, so we need to send an empty array so that communications
                # between processes stay synchronized.
                sync_array = np.zeros((0, self._output_size))
                self._gather_arrays_over_all_cpus(sync_array)
                continue

            samples = self._data.draw_samples(self._sample_sizes[level])
            num_samples = samples.shape[0]

            # If we've run short on samples, update sample sizes accordingly.
            self._cpu_sample_sizes[level] = num_samples

            # Input is out of samples or this cpu was not allocated any.
            if num_samples == 0:
                continue

            output_differences = np.zeros((num_samples, self._output_size))

            for i, sample in enumerate(samples):
                output_differences[i] = self._evaluate_sample(i, sample, level)

            all_output_differences = \
                self._gather_arrays_over_all_cpus(output_differences, axis=0)

            estimates += np.sum(all_output_differences, axis=0) / num_samples

            variances += np.var(all_output_differences, axis=0) / num_samples

        return estimates, variances

    def _evaluate_sample(self, i, sample, level):
        """
        Evaluate output of an input sample, either by running the model or
        retrieving the output from the cache. For levels > 0, returns
        difference between current level and lower level outputs.

        :param i: sample index
        :param sample: sample value
        :param level: model level
        :return: result of evaluation
        """
        if self._verbose and self._cpu_rank == 0:
            progress = str((float(i) / self._sample_sizes[level]) * 100)[:5]
            sys.stdout.write("\r                                              ")
            sys.stdout.write("\rLevel %s progress: %s%%" % (level, progress))

        # If we have the output for this sample cached, use it.
        # Otherwise, compute the output via the model.

        if self._initial_sample_size > 0:  # Avoid warnings in unit tests.

            # Absolute index of current sample.
            sample_index = np.sum(self._cpu_sample_sizes[:level]) + i

            # Level in cache that a sample with above index would be at.
            # This must match the current level.
            cpu_initial_sample_size = \
                self._determine_num_cpu_samples(self._initial_sample_size)
            cached_level = sample_index // cpu_initial_sample_size

            # Index within cached level for sample output.
            cached_index = sample_index - level * cpu_initial_sample_size

            # Level and index within cache must be correct for the
            # appropriate cached value to be found.
            can_use_cache = cached_index < cpu_initial_sample_size and \
                cached_level == level

            if can_use_cache:
                return self._cache[level, cached_index]

        # Compute output via the model.
        output = self._models[level].evaluate(sample)

        if level > 0:
            return output - self._models[level-1].evaluate(sample)
        else:
            return output

    def _show_summary_data(self, estimates, variances, run_time):
        """
        Shows summary of simulation.

        :param estimates: ndarray of estimates for each QoI.
        :param variances: ndarray of variances for each QoI.
        """
        # Compare variance for each quantity of interest to epsilon values.
        print
        print 'Total run time: %s' % str(run_time)
        print

        epsilons_squared = np.square(self._epsilons)
        for i, variance in enumerate(variances):

            passed = variance < epsilons_squared[i]
            estimate = estimates[i]

            print 'QOI #%s: estimate: %s, variance: %s, " + "' \
                  'epsilon^2: %s, success: %s' % \
                  (i, float(estimate), float(variance),
                   float(epsilons_squared[i]), passed)

    def _compute_costs_and_variances(self):
        """
        Compute costs and variances across levels.

        :return: tuple of ndarrays:
            1d ndarray of costs
            2d ndarray of variances
        """
        if self._verbose:
            sys.stdout.write("Determining costs: ")

        # Determine number of samples to be taken on this processor.
        self._cpu_initial_sample_size = \
            self._determine_num_cpu_samples(self._initial_sample_size)

        # Cache model outputs computed here so that they can be reused
        # in the simulation.
        self._cache = np.zeros((self._num_levels,
                                self._cpu_initial_sample_size,
                                self._output_size))

        # Process samples in model. Gather compute times for each level.
        # Variance is computed from difference between outputs of adjacent
        # layers evaluated from the same samples.
        compute_times = np.zeros(self._num_levels)

        for level in range(self._num_levels):

            input_samples = self._data.draw_samples(self._initial_sample_size)
            lower_level_outputs = np.zeros((self._cpu_initial_sample_size,
                                            self._output_size))

            start_time = timeit.default_timer()
            for i, sample in enumerate(input_samples):

                self._cache[level, i] = self._models[level].evaluate(sample)

                if level > 0:
                    lower_level_outputs[i] = \
                        self._models[level-1].evaluate(sample)

            compute_times[level] = timeit.default_timer() - start_time

            self._cache[level] -= lower_level_outputs

        # Get outputs across all CPUs before computing variances.
        all_outputs = self._gather_arrays_over_all_cpus(self._cache, axis=1)

        variances = np.var(all_outputs, axis=1)
        costs = self._compute_costs(compute_times)

        if self._verbose and self._cpu_rank == 0:
            print 'Initial sample variances: \n%s' % variances

        return costs, variances

    def _compute_costs(self, compute_times):
        """
        Set costs for each level, either from precomputed values from each
        model or based on computation times provided by compute_times.

        :param compute_times: ndarray of computation times for computing
        model at each layer and preceding layer.
        """
        costs = np.ones(self._num_levels)

        # If the models have costs predetermined, use them to compute costs
        # between each level.
        model_cost_defined = hasattr(self._models[0], 'cost')
        if model_cost_defined and self._models[0].cost is not None:

            for i, model in enumerate(self._models):
                costs[i] = model.cost

            # Costs at level > 0 should be summed with previous level.
            costs[1:] = costs[1:] + costs[:-1]

        else:
            # Compute costs based on compute time differences between levels.
            costs = compute_times / self._cpu_initial_sample_size

            # If costs are determined by time, we also need to multiply them
            # by the number of cpus so that they are based on total cost.
            costs *= self._num_cpus

        costs = self._mean_over_all_cpus(costs)

        # Save copy of costs for use in simulation as is.
        self._costs = np.copy(costs)

        if self._verbose:
            print np.array2string(costs)

        return costs

    def _determine_output_size(self):
        """
        Runs model on a small test sample to determine shape of output.
        """
        self._data.reset_sampling()
        test_sample = self._data.draw_samples(self._num_cpus)[0]
        self._data.reset_sampling()

        test_output = self._models[0].evaluate(test_sample)
        self._output_size = test_output.size

    def _compute_optimal_sample_sizes(self, costs, variances):
        """
        Compute the sample size for each level to be used in simulation.

        :param variances: 2d ndarray of variances
        :param costs: 1d ndarray of costs
        """
        if self._verbose:
            sys.stdout.write("Computing optimal sample sizes: ")

        # Need 2d version of costs in order to vectorize the operations.
        costs = costs[:, np.newaxis]

        # Compute mu.
        sum_sqrt_vc = np.sum(np.sqrt(variances * costs), axis=0)

        if self._target_cost is None:
            mu = np.power(self._epsilons, -2) * sum_sqrt_vc
        else:
            mu = self._target_cost * float(self._num_cpus) / sum_sqrt_vc

        # Compute sample sizes.
        sqrt_v_over_c = np.sqrt(variances / costs)
        self._sample_sizes = np.amax(np.trunc(mu * sqrt_v_over_c), axis=1)

        # Manually tweak sample sizes to get predicted cost closer to target.
        if self._target_cost is not None:
            self._fit_samples_sizes_to_target_cost(np.squeeze(costs))

        # Set sample sizes to ints.
        self._sample_sizes = self._sample_sizes.astype(int)

        # Divide sampling evenly across cpus.
        split_samples = np.vectorize(self._determine_num_cpu_samples)
        self._cpu_sample_sizes = split_samples(self._sample_sizes)

        print 'Sampling on %s:' % self._cpu_rank
        print self._sample_sizes
        print self._cpu_sample_sizes

        if self._verbose:

            print np.array2string(self._sample_sizes)

            estimated_runtime = np.dot(self._sample_sizes, np.squeeze(costs))

            self._show_time_estimate(estimated_runtime)

    def _fit_samples_sizes_to_target_cost(self, costs):
        """
        Adjust sample sizes to be as close to the target cost as possible.
        """
        # Find difference between projected total cost and target.
        total_cost = np.dot(costs, self._sample_sizes)
        difference = self._target_cost - total_cost

        # If the difference is greater than the lowest cost model, adjust
        # the sample sizes.
        if abs(difference) > costs[0]:

            # Start with highest cost model and add samples in order to fill
            # the cost gap as much as possible.
            for i in range(len(costs) - 1, -1, -1):
                if costs[i] < abs(difference):

                    # Compute number of samples we can fill the gap with at
                    # current level.
                    delta = np.trunc(difference / costs[i])
                    self._sample_sizes[i] += delta

                    if self._sample_sizes[i] < 0:
                        self._sample_sizes[i] = 0

                    # Update difference from target cost.
                    total_cost = np.sum(costs * self._sample_sizes)
                    difference = self._target_cost - total_cost

        # If target cost is less than cost of least expensive model, run it
        # once so we are at least doing something in the simulation.
        if np.sum(self._sample_sizes) == 0.:
            self._sample_sizes[0] = 1

    def _process_epsilon(self, epsilon):
        """
        Produce an ndarray of epsilon values from scalar or vector of epsilons.
        If a vector, length should match the number of quantities of interest.

        :param epsilon: float, list of floats, or ndarray.
        :return: ndarray of epsilons of size (self.num_levels).
        """
        if isinstance(epsilon, list):
            epsilon = np.array(epsilon)

        if isinstance(epsilon, float):
            epsilon = np.ones(self._output_size) * epsilon

        if not isinstance(epsilon, np.ndarray):
            raise TypeError("Epsilon must be a float, list of floats, " +
                            "or an ndarray.")

        if np.any(epsilon <= 0.):
            raise ValueError("Epsilon values must be greater than 0.")

        if len(epsilon) != self._output_size:
            raise ValueError("Number of epsilons must match number of levels.")

        return epsilon

    def __check_init_parameters(self, data, models):

        if not isinstance(data, Input):
            TypeError("data must inherit from Input class.")

        if not isinstance(models, list):
            TypeError("models must be a list of models.")

        # Reset sampling in case input data is used more than once.
        data.reset_sampling()

        # Ensure all models have the same output dimensions.
        output_sizes = []
        test_sample = data.draw_samples(self._num_cpus)[0]
        data.reset_sampling()

        for model in models:
            if not isinstance(model, Model):
                TypeError("models must be a list of models.")

            test_output = model.evaluate(test_sample)
            output_sizes.append(test_output.size)

        output_sizes = np.array(output_sizes)
        if not np.all(output_sizes == output_sizes[0]):
            raise ValueError("All models must return the same output " +
                             "dimensions.")

    @staticmethod
    def __check_simulate_parameters(starting_sample_size, target_cost):

        if not isinstance(starting_sample_size, int):
            raise TypeError("starting_sample_size must be an integer.")

        if starting_sample_size < 1:
            raise ValueError("starting_sample_size must be greater than zero.")

        if target_cost is not None:

            if not (isinstance(target_cost, float) or
                    isinstance(target_cost, int)):

                raise TypeError('maximum cost must be an int or float.')

            if target_cost <= 0:
                raise ValueError("maximum cost must be greater than zero.")

    def __detect_parallelization(self):
        """
        Detects whether multiple processors are available and sets
        self.number_CPUs and self.cpu_rank accordingly.
        """
        try:
            imp.find_module('mpi4py')

            from mpi4py import MPI
            comm = MPI.COMM_WORLD

            self._num_cpus = comm.size
            self._cpu_rank = comm.rank
            self._comm = comm

        except ImportError:

            self._num_cpus = 1
            self._cpu_rank = 0

    def _mean_over_all_cpus(self, this_cpu_values, axis=0):
        """
        Finds the mean of ndarray of values across cpus and returns result.
        :param this_cpu_values: ndarray of any shape.
        :return: ndarray of same shape as values with mean from all cpus.
        """
        if self._num_cpus == 1:
            return this_cpu_values

        all_values = self._comm.allgather(this_cpu_values)

        return np.mean(all_values, axis)

    def _sum_over_all_cpus(self, this_cpu_values, axis=0):

        if self._num_cpus == 1:
            return this_cpu_values

        all_values = self._comm.allgather(this_cpu_values)

        return np.sum(all_values, axis)

    def _gather_arrays_over_all_cpus(self, this_cpu_array, axis=0):
        """
        Collects an array from all processes and combines them so that single
        processor ordering is preserved.
        :param this_cpu_array: Arrays to be combined.
        :param axis: Axis to concatenate the arrays on.
        :return: ndarray
        """
        if self._num_cpus == 1:
            return this_cpu_array

        gathered_arrays = self._comm.allgather(this_cpu_array)

        # Remove arrays with no data (sent to prevent sync issues).
        all_arrays = list()
        for array in gathered_arrays:
            if array.shape[axis] > 0:
                all_arrays.append(array)

        if len(all_arrays) == 0:
            return this_cpu_array

        return np.concatenate(all_arrays, axis=axis)

    def _determine_num_cpu_samples(self, total_num_samples):
        """Determines number of samples to be run on current cpu based on
            total number of samples to be run.
            :param total_num_samples: Total samples to be taken.
            :return: Samples to be taken by this cpu.
        """
        num_cpu_samples = total_num_samples // self._num_cpus

        num_residual_samples = total_num_samples - \
            num_cpu_samples * self._num_cpus

        if self._cpu_rank < num_residual_samples:
            num_cpu_samples += 1

        return num_cpu_samples

    @staticmethod
    def _show_time_estimate(seconds):

        time_delta = timedelta(seconds=seconds)

        print 'Estimated simulation time: %s' % str(time_delta)
