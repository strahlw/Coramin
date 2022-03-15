import math
from coramin.relaxations.relaxations_base import (
    BaseRelaxationData,
    BasePWRelaxationData,
)
import pyomo.environ as pe
from pyomo.common.timing import HierarchicalTimer
from pyomo.core.base.block import _BlockData
from pyomo.contrib.appsi.base import (
    Results,
    PersistentSolver,
    Solver,
    MIPSolverConfig,
    TerminationCondition,
    SolutionLoaderBase,
    UpdateConfig,
)
from pyomo.contrib import appsi
from typing import Tuple, Optional, MutableMapping, Sequence
from pyomo.common.config import ConfigValue, NonNegativeInt, PositiveFloat, PositiveInt
import logging
from coramin.relaxations.auto_relax import relax
from coramin.relaxations.iterators import (
    relaxation_data_objects,
    nonrelaxation_component_data_objects,
)
from coramin.utils.coramin_enums import RelaxationSide
from coramin.domain_reduction import push_integers, pop_integers, collect_vars_to_tighten, perform_obbt
import time
from pyomo.core.base.var import _GeneralVarData
from pyomo.core.base.objective import _GeneralObjectiveData
from coramin.utils.pyomo_utils import get_objective
from pyomo.common.collections.component_set import ComponentSet
from pyomo.common.modeling import unique_component_name
from pyomo.common.errors import InfeasibleConstraintException
from pyomo.contrib.fbbt.fbbt import BoundsManager


logger = logging.getLogger(__name__)


class MultiTreeConfig(MIPSolverConfig):
    def __init__(
        self,
        description=None,
        doc=None,
        implicit=False,
        implicit_domain=None,
        visibility=0,
    ):
        super(MultiTreeConfig, self).__init__(
            description=description,
            doc=doc,
            implicit=implicit,
            implicit_domain=implicit_domain,
            visibility=visibility,
        )

        self.declare("solver_output_logger", ConfigValue())
        self.declare("log_level", ConfigValue(domain=NonNegativeInt))
        self.declare("feasibility_tolerance", ConfigValue(domain=PositiveFloat))
        self.declare("abs_gap", ConfigValue(domain=PositiveFloat))
        self.declare("max_partitions_per_iter", ConfigValue(domain=PositiveInt))
        self.declare("max_iter", ConfigValue(domain=NonNegativeInt))
        self.declare("root_obbt_max_iter", ConfigValue(domain=NonNegativeInt))

        self.solver_output_logger = logger
        self.log_level = logging.INFO
        self.feasibility_tolerance = 1e-6
        self.time_limit = 600
        self.abs_gap = 1e-4
        self.mip_gap = 0.001
        self.max_partitions_per_iter = 100000
        self.max_iter = 100
        self.root_obbt_max_iter = 3


def _is_problem_definitely_convex(m: _BlockData) -> bool:
    res = True
    for r in relaxation_data_objects(m, descend_into=True, active=True):
        if r.relaxation_side == RelaxationSide.BOTH:
            res = False
            break
        elif r.relaxation_side == RelaxationSide.UNDER and not r.is_rhs_convex():
            res = False
            break
        elif r.relaxation_side == RelaxationSide.OVER and not r.is_rhs_concave():
            res = False
            break
    return res


class MultiTreeResults(Results):
    def __init__(self):
        super().__init__()
        self.wallclock_time = None


class MultiTreeSolutionLoader(SolutionLoaderBase):
    def __init__(self, primals: MutableMapping):
        self._primals = primals

    def get_primals(
        self, vars_to_load: Optional[Sequence[_GeneralVarData]] = None
    ) -> MutableMapping[_GeneralVarData, float]:
        if vars_to_load is None:
            return pe.ComponentMap(self._primals.items())
        else:
            primals = pe.ComponentMap()
            for v in vars_to_load:
                primals[v] = self._primals[v]
        return primals


class MultiTree(Solver):
    def __init__(self, mip_solver: PersistentSolver, nlp_solver: PersistentSolver):
        super(MultiTree, self).__init__()
        self._config = MultiTreeConfig()
        self.mip_solver: PersistentSolver = mip_solver
        self.nlp_solver: PersistentSolver = nlp_solver
        self._original_model: Optional[_BlockData] = None
        self._relaxation: Optional[_BlockData] = None
        self._nlp: Optional[_BlockData] = None
        self._start_time: Optional[float] = None
        self._incumbent: Optional[pe.ComponentMap] = None
        self._best_feasible_objective: Optional[float] = None
        self._best_objective_bound: Optional[float] = None
        self._objective: Optional[_GeneralObjectiveData] = None
        self._relaxation_objects: Optional[Sequence[BaseRelaxationData]] = None
        self._stop: Optional[TerminationCondition] = None
        self._discrete_vars: Optional[Sequence[_GeneralVarData]] = None
        self._rel_to_nlp_map: Optional[MutableMapping] = None
        self._nlp_to_orig_map: Optional[MutableMapping] = None
        self._nlp_tightener: Optional[appsi.fbbt.IntervalTightener] = None
        self._nlp_termination: TerminationCondition = TerminationCondition.unknown
        self._rel_termination: TerminationCondition = TerminationCondition.unknown
        self._iter: int = 0

    def _re_init(self):
        self._original_model: Optional[_BlockData] = None
        self._relaxation: Optional[_BlockData] = None
        self._nlp: Optional[_BlockData] = None
        self._start_time: Optional[float] = None
        self._incumbent: Optional[pe.ComponentMap] = None
        self._best_feasible_objective: Optional[float] = None
        self._best_objective_bound: Optional[float] = None
        self._objective: Optional[_GeneralObjectiveData] = None
        self._relaxation_objects: Optional[Sequence[BaseRelaxationData]] = None
        self._stop: Optional[TerminationCondition] = None
        self._discrete_vars: Optional[Sequence[_GeneralVarData]] = None
        self._rel_to_nlp_map: Optional[MutableMapping] = None
        self._nlp_to_orig_map: Optional[MutableMapping] = None
        self._nlp_tightener: Optional[appsi.fbbt.IntervalTightener] = None
        self._nlp_termination: TerminationCondition = TerminationCondition.unknown
        self._rel_termination: TerminationCondition = TerminationCondition.unknown
        self._iter: int = 0

    def available(self):
        if (
            self.mip_solver.available() == Solver.Availability.FullLicense
            and self.nlp_solver.available() == Solver.Availability.FullLicense
        ):
            return Solver.Availability.FullLicense
        elif self.mip_solver.available() == Solver.Availability.FullLicense:
            return self.nlp_solver.available()
        else:
            return self.mip_solver.available()

    def version(self) -> Tuple:
        return 0, 1, 0

    @property
    def config(self) -> MultiTreeConfig:
        return self._config

    @config.setter
    def config(self, val: MultiTreeConfig):
        self._config = val

    @property
    def symbol_map(self):
        raise NotImplementedError("This solver does not have a symbol map")

    def _should_terminate(self) -> Tuple[bool, Optional[TerminationCondition]]:
        if self._elapsed_time >= self.config.time_limit:
            return True, TerminationCondition.maxTimeLimit
        if self._iter >= self.config.max_iter:
            return True, TerminationCondition.maxIterations
        if self._stop is not None:
            return True, self._stop
        primal_bound = self._get_primal_bound()
        dual_bound = self._get_dual_bound()
        if self._objective.sense == pe.minimize:
            assert primal_bound >= dual_bound - 1e-6*max(abs(primal_bound), abs(dual_bound)) - 1e-6
        else:
            assert primal_bound <= dual_bound + 1e-6*max(abs(primal_bound), abs(dual_bound)) + 1e-6
        abs_gap, rel_gap = self._get_abs_and_rel_gap()
        if abs_gap <= self.config.abs_gap:
            return True, TerminationCondition.optimal
        if rel_gap <= self.config.mip_gap:
            return True, TerminationCondition.optimal
        return False, TerminationCondition.unknown

    def _get_results(self, termination_condition: TerminationCondition) -> MultiTreeResults:
        res = MultiTreeResults()
        res.termination_condition = termination_condition
        res.best_feasible_objective = self._best_feasible_objective
        res.best_objective_bound = self._best_objective_bound
        if self._best_feasible_objective is not None:
            res.solution_loader = MultiTreeSolutionLoader(self._incumbent)
        res.wallclock_time = self._elapsed_time

        if self.config.load_solution:
            if res.best_feasible_objective is not None:
                if res.termination_condition != TerminationCondition.optimal:
                    logger.warning('Loading a feasible but potentially sub-optimal '
                                   'solution. Please check the termination condition.')
                res.solution_loader.load_vars()
            else:
                raise RuntimeError('No feasible solution was found. Please '
                                   'set opt.config.load_solution=False and check the '
                                   'termination condition before loading a solution.')

        return res

    def _get_primal_bound(self) -> float:
        if self._best_feasible_objective is None:
            if self._objective.sense == pe.minimize:
                primal_bound = math.inf
            else:
                primal_bound = -math.inf
        else:
            primal_bound = self._best_feasible_objective
        return primal_bound

    def _get_dual_bound(self) -> float:
        if self._best_objective_bound is None:
            if self._objective.sense == pe.minimize:
                dual_bound = -math.inf
            else:
                dual_bound = math.inf
        else:
            dual_bound = self._best_objective_bound
        return dual_bound

    def _get_abs_and_rel_gap(self):
        primal_bound = self._get_primal_bound()
        dual_bound = self._get_dual_bound()
        abs_gap = abs(primal_bound - dual_bound)
        if abs_gap == 0:
            rel_gap = 0
        elif primal_bound == 0:
            rel_gap = math.inf
        elif math.isinf(abs_gap):
            rel_gap = math.inf
        else:
            rel_gap = abs_gap / abs(primal_bound)
        return abs_gap, rel_gap

    def _get_constr_violation(self):
        viol_list = list()
        for b in self._relaxation_objects:
            any_none = False
            for v in b.get_rhs_vars():
                if v.value is None:
                    any_none = True
                    break
            if any_none:
                viol_list.append(math.inf)
                break
            else:
                viol_list.append(b.get_deviation())
        return max(viol_list)

    def _log(self, header=False):
        logger = self.config.solver_output_logger
        log_level = self.config.log_level
        if header:
            logger.log(
                log_level,
                f"    {'Iter':<10}{'Primal Bound':<15}{'Dual Bound':<15}{'Abs Gap':<15}"
                f"{'Rel Gap':<15}{'Constr Viol':<15}{'Time':<15}{'NLP Term':<15}"
                f"{'Rel Term':<15}",
            )
        else:
            primal_bound = self._get_primal_bound()
            dual_bound = self._get_dual_bound()
            abs_gap, rel_gap = self._get_abs_and_rel_gap()
            if self._best_objective_bound is not None:
                constr_viol = self._get_constr_violation()
            else:
                constr_viol = math.inf
            logger.log(
                log_level,
                f"    {self._iter:<10}{primal_bound:<15.3e}{dual_bound:<15.3e}"
                f"{abs_gap:<15.3e}{rel_gap:<15.3f}{constr_viol:<15.3e}"
                f"{self._elapsed_time:<15.2f}{str(self._nlp_termination.name):<15}"
                f"{str(self._rel_termination.name):<15}",
            )

    def _update_dual_bound(self, res: Results):
        if res.best_objective_bound is not None:
            if self._objective.sense == pe.minimize:
                if (
                    self._best_objective_bound is None
                    or res.best_objective_bound > self._best_objective_bound
                ):
                    self._best_objective_bound = res.best_objective_bound
            else:
                if (
                    self._best_objective_bound is None
                    or res.best_objective_bound < self._best_objective_bound
                ):
                    self._best_objective_bound = res.best_objective_bound

        if res.best_feasible_objective is not None:
            max_viol = self._get_constr_violation()
            if max_viol > self.config.feasibility_tolerance:
                all_cons_satisfied = False
            else:
                all_cons_satisfied = True
            if all_cons_satisfied:
                for v in self._discrete_vars:
                    if not math.isclose(v.value, round(v.value)):
                        all_cons_satisfied = False
                        break
            if all_cons_satisfied:
                for rel_v, nlp_v in self._rel_to_nlp_map.items():
                    nlp_v.value = rel_v.value
                self._update_primal_bound(res)

    def _update_primal_bound(self, res: Results):
        should_update = False
        if res.best_feasible_objective is not None:
            if self._objective.sense == pe.minimize:
                if (
                    self._best_feasible_objective is None
                    or res.best_feasible_objective < self._best_feasible_objective
                ):
                    should_update = True
            else:
                if (
                    self._best_feasible_objective is None
                    or res.best_feasible_objective > self._best_feasible_objective
                ):
                    should_update = True

        if should_update:
            self._best_feasible_objective = res.best_feasible_objective
            self._incumbent = pe.ComponentMap()
            for nlp_v, orig_v in self._nlp_to_orig_map.items():
                self._incumbent[orig_v] = nlp_v.value

    def _solve_nlp_with_fixed_vars(
        self,
        integer_var_values: MutableMapping[_GeneralVarData, float],
        rhs_var_bounds: MutableMapping[_GeneralVarData, Tuple[float, float]],
    ) -> Results:
        self._iter += 1

        bm = BoundsManager(self._nlp)
        bm.save_bounds()

        fixed_vars = list()
        for v in self._discrete_vars:
            if v.fixed:
                continue
            val = integer_var_values[v]
            assert math.isclose(val, round(val), rel_tol=1e-6, abs_tol=1e-6)
            val = round(val)
            nlp_v = self._rel_to_nlp_map[v]
            nlp_v.fix(val)
            fixed_vars.append(nlp_v)

        for v, (v_lb, v_ub) in rhs_var_bounds.items():
            if v.fixed:
                continue
            nlp_v = self._rel_to_nlp_map[v]
            nlp_v.setlb(v_lb)
            nlp_v.setub(v_ub)

        nlp_res = Results()

        active_constraints = list()
        for c in ComponentSet(
            self._nlp.component_data_objects(
                pe.Constraint, active=True, descend_into=True
            )
        ):
            active_constraints.append(c)

        try:
            self._nlp_tightener.perform_fbbt(self._nlp)
            proven_infeasible = False
        except InfeasibleConstraintException:
            logger.info("NLP proven infeasiblee with FBBT")
            nlp_res = Results()
            nlp_res.termination_condition = TerminationCondition.infeasible
            proven_infeasible = True

        if not proven_infeasible:
            for v in ComponentSet(
                self._nlp.component_data_objects(pe.Var, descend_into=True)
            ):
                if v.fixed:
                    continue
                if v.has_lb() and v.has_ub():
                    if math.isclose(v.lb, v.ub):
                        v.fix(0.5 * (v.lb + v.ub))
                        fixed_vars.append(v)
                    else:
                        v.value = 0.5 * (v.lb + v.ub)

            any_unfixed_vars = False
            for v in self._nlp.component_data_objects(
                pe.Var, descend_into=True
            ):
                if not v.fixed:
                    any_unfixed_vars = True
                    break

            if any_unfixed_vars:
                self.nlp_solver.config.time_limit = self._remaining_time
                self.nlp_solver.config.load_solution = False
                nlp_res = self.nlp_solver.solve(self._nlp)
                if nlp_res.best_feasible_objective is not None:
                    nlp_res.solution_loader.load_vars()
            else:
                nlp_obj = get_objective(self._nlp)
                # there should not be any active constraints
                # they should all have been deactivated by FBBT
                for c in active_constraints:
                    assert not c.active
                nlp_res.termination_condition = TerminationCondition.optimal
                nlp_res.best_feasible_objective = pe.value(nlp_obj)
                nlp_res.best_objective_bound = nlp_res.best_feasible_objective
                nlp_res.solution_loader = MultiTreeSolutionLoader(pe.ComponentMap((v, v.value) for v in self._nlp.component_data_objects(pe.Var, descend_into=True)))

        self._nlp_termination = nlp_res.termination_condition

        self._update_primal_bound(nlp_res)
        self._log(header=False)

        for v in fixed_vars:
            v.unfix()

        bm.pop_bounds()

        for c in active_constraints:
            c.activate()

        return nlp_res

    def _solve_relaxation(self) -> Results:
        self._iter += 1
        self.mip_solver.config.time_limit = self._remaining_time
        self.mip_solver.config.load_solution = False
        rel_res = self.mip_solver.solve(self._relaxation)

        if rel_res.best_feasible_objective is not None:
            rel_res.solution_loader.load_vars()

        self._update_dual_bound(rel_res)
        self._log(header=False)
        if rel_res.termination_condition not in {
            TerminationCondition.optimal,
            TerminationCondition.maxTimeLimit,
            TerminationCondition.maxIterations,
            TerminationCondition.objectiveLimit,
            TerminationCondition.interrupted,
        }:
            self._stop = rel_res.termination_condition
        self._rel_termination = rel_res.termination_condition
        return rel_res

    def _partition_helper(self):
        dev_list = list()

        err = False

        for b in self._relaxation_objects:
            for v in b.get_rhs_vars():
                if not v.has_lb() or not v.has_ub():
                    logger.error(
                        'The multitree algorithm is not guaranteed to converge '
                        'for problems with unbounded variables. Please bound all '
                        'variables.')
                    self._stop = TerminationCondition.error
                    err = True
                    break
            if err:
                break

            aux_val = b.get_aux_var().value
            rhs_val = pe.value(b.get_rhs_expr())
            if (
                aux_val > rhs_val + self.config.feasibility_tolerance
                and b.relaxation_side in {RelaxationSide.BOTH, RelaxationSide.OVER}
                and not b.is_rhs_concave()
            ):
                dev_list.append((b, aux_val - rhs_val))
            elif (
                aux_val < rhs_val - self.config.feasibility_tolerance
                and b.relaxation_side in {RelaxationSide.BOTH, RelaxationSide.UNDER}
                and not b.is_rhs_convex()
            ):
                dev_list.append((b, rhs_val - aux_val))

        if not err:
            dev_list.sort(key=lambda x: x[1], reverse=True)

            for b, dev in dev_list[: self.config.max_partitions_per_iter]:
                b.add_partition_point()
                b.rebuild()

    def _oa_cut_helper(self, tol):
        new_con_list = list()
        for b in self._relaxation_objects:
            new_con = b.add_cut(
                keep_cut=True, check_violation=True, feasibility_tol=tol
            )
            if new_con is not None:
                new_con_list.append(new_con)
        self.mip_solver.add_constraints(new_con_list)
        return new_con_list

    def _add_oa_cuts(self, tol, max_iter) -> Results:
        original_update_config: UpdateConfig = self.mip_solver.update_config()

        self.mip_solver.update()

        self.mip_solver.update_config.update_params = False
        self.mip_solver.update_config.update_vars = False
        self.mip_solver.update_config.update_objective = False
        self.mip_solver.update_config.update_constraints = False
        self.mip_solver.update_config.check_for_new_objective = False
        self.mip_solver.update_config.check_for_new_or_removed_constraints = False
        self.mip_solver.update_config.check_for_new_or_removed_vars = False
        self.mip_solver.update_config.check_for_new_or_removed_params = True
        self.mip_solver.update_config.treat_fixed_vars_as_params = True
        self.mip_solver.update_config.update_named_expressions = False

        last_res = None

        for _iter in range(max_iter):
            if self._should_terminate()[0]:
                break

            rel_res = self._solve_relaxation()
            if rel_res.best_feasible_objective is not None:
                last_res = Results()
                last_res.best_feasible_objective = rel_res.best_feasible_objective
                last_res.best_objective_bound = rel_res.best_objective_bound
                last_res.termination_condition = rel_res.termination_condition
                last_res.solution_loader = MultiTreeSolutionLoader(
                    rel_res.solution_loader.get_primals(
                        vars_to_load=self._discrete_vars
                    )
                )

            if self._should_terminate()[0]:
                break

            new_con_list = self._oa_cut_helper(tol=tol)
            if len(new_con_list) == 0:
                break

        self.mip_solver.update_config.update_params = (
            original_update_config.update_params
        )
        self.mip_solver.update_config.update_vars = original_update_config.update_vars
        self.mip_solver.update_config.update_objective = (
            original_update_config.update_objective
        )
        self.mip_solver.update_config.update_constraints = (
            original_update_config.update_constraints
        )
        self.mip_solver.update_config.check_for_new_objective = (
            original_update_config.check_for_new_objective
        )
        self.mip_solver.update_config.check_for_new_or_removed_constraints = (
            original_update_config.check_for_new_or_removed_constraints
        )
        self.mip_solver.update_config.check_for_new_or_removed_vars = (
            original_update_config.check_for_new_or_removed_vars
        )
        self.mip_solver.update_config.check_for_new_or_removed_params = (
            original_update_config.check_for_new_or_removed_params
        )
        self.mip_solver.update_config.treat_fixed_vars_as_params = (
            original_update_config.treat_fixed_vars_as_params
        )
        self.mip_solver.update_config.update_named_expressions = (
            original_update_config.update_named_expressions
        )

        if last_res is None:
            last_res = Results()

        return last_res

    def _construct_nlp(self):
        all_vars = list(
            ComponentSet(
                self._original_model.component_data_objects(pe.Var, descend_into=True)
            )
        )
        tmp_name = unique_component_name(self._original_model, "all_vars")
        setattr(self._original_model, tmp_name, all_vars)
        self._nlp = relax(
            model=self._original_model,
            in_place=False,
            use_fbbt=True,
            fbbt_options={"deactivate_satisfied_constraints": True, "max_iter": 2},
        )
        new_vars = getattr(self._nlp, tmp_name)
        self._nlp_to_orig_map = pe.ComponentMap(zip(new_vars, all_vars))
        delattr(self._original_model, tmp_name)
        delattr(self._nlp, tmp_name)

        for b in relaxation_data_objects(self._nlp, descend_into=True, active=True):
            b.rebuild(build_nonlinear_constraint=True)

    def _construct_relaxation(self):
        all_vars = list(
            ComponentSet(
                self._nlp.component_data_objects(pe.Var, descend_into=True)
            )
        )
        tmp_name = unique_component_name(self._nlp, "all_vars")
        setattr(self._nlp, tmp_name, all_vars)
        self._relaxation = self._nlp.clone()
        new_vars = getattr(self._relaxation, tmp_name)
        self._rel_to_nlp_map = pe.ComponentMap(zip(new_vars, all_vars))
        delattr(self._nlp, tmp_name)
        delattr(self._relaxation, tmp_name)

        for b in relaxation_data_objects(self._relaxation, descend_into=True, active=True):
            b.rebuild()

    def _get_nlp_specs_from_rel(self):
        integer_var_values = pe.ComponentMap()
        for v in self._discrete_vars:
            integer_var_values[v] = v.value
        rhs_var_bounds = pe.ComponentMap()
        for r in self._relaxation_objects:
            if not isinstance(r, BasePWRelaxationData):
                continue
            any_unbounded_vars = False
            for v in r.get_rhs_vars():
                if not v.has_lb() or not v.has_ub():
                    any_unbounded_vars = True
                    break
            if any_unbounded_vars:
                continue
            active_parts = r.get_active_partitions()
            assert len(active_parts) == 1
            v, bnds = list(active_parts.items())[0]
            if v in rhs_var_bounds:
                existing_bnds = rhs_var_bounds[v]
                bnds = (max(bnds[0], existing_bnds[0]), min(bnds[1], existing_bnds[1]))
            assert bnds[0] <= bnds[1]
            rhs_var_bounds[v] = bnds
        return integer_var_values, rhs_var_bounds

    @property
    def _elapsed_time(self):
        return time.time() - self._start_time

    @property
    def _remaining_time(self):
        return max(0, self.config.time_limit - self._elapsed_time)

    def solve(self, model: _BlockData, timer: HierarchicalTimer = None) -> MultiTreeResults:
        self._re_init()

        self._start_time = time.time()
        if timer is None:
            timer = HierarchicalTimer()
        timer.start("solve")

        self._original_model = model

        self._log(header=True)

        timer.start("construct relaxation")
        self._construct_nlp()
        self._construct_relaxation()
        timer.stop("construct relaxation")

        self._objective = get_objective(self._relaxation)
        self._relaxation_objects = list()
        for r in relaxation_data_objects(
            self._relaxation, descend_into=True, active=True
        ):
            self._relaxation_objects.append(r)

        should_terminate, reason = self._should_terminate()
        if should_terminate:
            return self._get_results(reason)

        self._log(header=False)

        self.mip_solver.set_instance(self._relaxation)
        self._nlp_tightener = appsi.fbbt.IntervalTightener()
        self._nlp_tightener.config.deactivate_satisfied_constraints = True
        self._nlp_tightener.config.feasibility_tol = self.config.feasibility_tolerance
        self._nlp_tightener.set_instance(self._nlp, symbolic_solver_labels=False)

        relaxed_binaries, relaxed_integers = push_integers(self._relaxation)
        self._discrete_vars = list(relaxed_binaries) + list(relaxed_integers)
        oa_results = self._add_oa_cuts(self.config.feasibility_tolerance * 100, 100)
        pop_integers(relaxed_binaries, relaxed_integers)

        should_terminate, reason = self._should_terminate()
        if should_terminate:
            return self._get_results(reason)

        if _is_problem_definitely_convex(self._relaxation):
            oa_results = self._add_oa_cuts(self.config.feasibility_tolerance, 100)
        else:
            oa_results = self._add_oa_cuts(self.config.feasibility_tolerance * 1e3, 3)

        should_terminate, reason = self._should_terminate()
        if should_terminate:
            return self._get_results(reason)

        if oa_results.best_feasible_objective is not None:
            integer_var_values, rhs_var_bounds = self._get_nlp_specs_from_rel()
            nlp_res = self._solve_nlp_with_fixed_vars(
                integer_var_values, rhs_var_bounds
            )

        vars_to_tighten = collect_vars_to_tighten(self._relaxation)
        relaxed_binaries, relaxed_integers = push_integers(self._relaxation)
        obbt_opt = pe.SolverFactory('gurobi_persistent')
        for obbt_iter in range(self.config.root_obbt_max_iter):
            perform_obbt(self._relaxation, solver=obbt_opt, varlist=list(vars_to_tighten),
                         objective_bound=self._best_feasible_objective, with_progress_bar=False,
                         time_limit=self._remaining_time)
            for r in self._relaxation_objects:
                r.rebuild()
        pop_integers(relaxed_binaries, relaxed_integers)

        while True:
            should_terminate, reason = self._should_terminate()
            if should_terminate:
                break
            
            rel_res = self._solve_relaxation()

            should_terminate, reason = self._should_terminate()
            if should_terminate:
                break

            if rel_res.best_feasible_objective is not None:
                integer_var_values, rhs_var_bounds = self._get_nlp_specs_from_rel()
                nlp_res = self._solve_nlp_with_fixed_vars(
                    integer_var_values, rhs_var_bounds
                )
                self._oa_cut_helper(self.config.feasibility_tolerance)
                self._partition_helper()
            else:
                self.config.solver_output_logger.warning(
                    f"relaxation did not find a feasible solution: "
                    f"{rel_res.termination_condition}"
                )

        res = self._get_results(reason)

        timer.stop("solve")

        return res
