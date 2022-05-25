# -*- coding: utf-8 -*-
"""DurableConsumptionModel

Solves a consumption-saving model with a durable consumption good and non-convex adjustment costs with either:

A. vfi: value function iteration (only i C++)
B. nvfi: nested value function iteration (both in Python and C++)
C. negm: nested endogenous grid point method (both in Python and C++)

"""

##############
# 1. imports #
##############

import time
import numpy as np

# consav package
from consav import ModelClass, jit # baseline model class and jit

from consav.grids import nonlinspace # grids
from quadrature import create_PT_shocks, normal_gauss_hermite # income shocks

# local modules
import last_period
import post_decision
import vfi
import nvfi
import negm
import simulate
import figs

class DurableConsumptionModelClass(ModelClass):
    
    #########
    # setup #
    #########
    
    def settings(self):
        """ choose settings """

        # a. namespaces
        self.namespaces = []
        
        # b. other attributes
        self.other_attrs = []
        
        # c. savefolder
        self.savefolder = 'saved'

        # d. list not-floats for safe type inference
        self.not_floats = ['housing_shock','solmethod','T','t','simN','sim_seed',
                           'Npsi','Nxi','Nz','Nm','Np','Nn','Nx','Na','Nshocks',
                           'do_print','do_print_period','do_marg_u']

    def setup(self):
        """ set baseline parameters """

        par = self.par

        # a. baseline parameters
        
        # horizon and life cycle
        par.Tmin = 0 # age when entering the model
        par.T = 40 - par.Tmin # age of death
        par.Tr = 30 - par.Tmin # retirement age
        par.G = 1.02 # growth in permanent income
        par.L = np.ones(par.T-1)
        par.L[0:par.Tr] = np.linspace(1,1/par.G,par.Tr) 
        par.L[par.Tr-1] = 0.67 # drop in permanent income at retirement age
        par.L[par.Tr-1:] = par.L[par.Tr-1:]/par.G # constant permanent income after retirement
        
        # preferences
        par.beta = 0.965
        par.rho = 2.0
        par.alpha = 0.9
        par.d_ubar = 1e-2
        par.theta = 0.8

        # returns and income
        par.housing_shock = True
        par.R = 1.03
        par.Rh = par.R + 0.05
        par.tau = 0.10
        par.gamma = 0 # 0.05 
        par.delta = 0.15
        par.sigma_psi = 0.1
        par.sigma_xi = 0.1
        par.sigma_epsilon = 0.02
        par.Nz = 5 
        par.Npsi = 5
        par.Nxi = 5
        par.pi = -0.1
        par.mu = 0.5    # probably remove this
        par.omega = np.zeros(par.T)
        
        # grids
        par.Np = 50
        par.p_min = 1e-4
        par.p_max = 3.0
        par.Nn = 50
        par.n_max = 3.0
        par.Nm = 100
        par.m_max = 10.0    
        par.Nx = 100
        par.x_max = par.m_max + par.n_max
        par.Na = 100
        par.a_max = par.m_max+1.0

        # simulation
        par.sigma_p0 = 0.2
        par.mu_d0 = 0.8
        par.sigma_d0 = 0.2
        par.mu_a0 = 0.2
        par.sigma_a0 = 0.1
        par.simN = 5000
        par.sim_seed = 1998
        par.euler_cutoff = 0.02
        par.moments_minage = 10
        par.moments_maxage = 35
        par.moments_numsim = 1

        # misc
        par.solmethod = 'nvfi'
        par.t = 0
        par.tol = 1e-8
        par.do_print = False
        par.do_print_period = False
        par.do_simple_wq = False # not using optimized interpolation in C++
        par.do_marg_u = False # calculate marginal utility for use in egm
        
    def allocate(self):
        """ allocate model, i.e. create grids and allocate solution and simluation arrays """

        self.create_grids()
        self.solve_prep()
        self.simulate_prep()
            
    def create_grids(self):
        """ construct grids for states and shocks """
        
        par = self.par

        if par.solmethod == 'negm':
            par.do_marg_u = True

        # a. states        
        par.grid_p = nonlinspace(par.p_min,par.p_max,par.Np,1.1)
        par.grid_n = nonlinspace(0,par.n_max,par.Nn,1.1)
        par.grid_m = nonlinspace(0,par.m_max,par.Nm,1.1)
        par.grid_x = nonlinspace(0,par.x_max,par.Nx,1.1)
        
        # b. post-decision states
        #par.grid_a = nonlinspace(0,par.a_max,par.Na,1.1)
        par.grid_a = np.nan + np.zeros((par.Nn,par.Na))
        
        for i_n in range(par.Nn): 
           par.grid_a[i_n,:] = nonlinspace(-par.omega[0]*par.grid_n[i_n],par.a_max,par.Na,1.1)
        
        # c. shocks

        shocks = create_PT_shocks(
            sigma_psi=par.sigma_psi,
            Npsi=par.Npsi,
            sigma_xi=par.sigma_xi,
            Nxi=par.Nxi,
            sigma_epsilon=par.sigma_epsilon,
            Nz=par.Nz,
            gamma=par.gamma,
            pi=par.pi,
            )
        par.psi,par.psi_w,par.xi,par.xi_w,par.z,par.z_w,par.Nshocks = shocks

        # d. set seed
        np.random.seed(par.sim_seed)

        # e. timing
        par.time_w = np.zeros(par.T)
        par.time_keep = np.zeros(par.T)
        par.time_adj = np.zeros(par.T)
        par.time_adj_full = np.zeros(par.T)

    #########
    # solve #
    #########

    def precompile_numba(self):
        """ solve the model with very coarse grids and simulate with very few persons"""

        par = self.par

        tic = time.time()

        # a. define
        fastpar = dict()
        fastpar['do_print'] = False
        fastpar['do_print_period'] = False
        fastpar['T'] = 2
        fastpar['Np'] = 3
        fastpar['Nn'] = 3
        fastpar['Nm'] = 3
        fastpar['Nx'] = 3
        fastpar['Na'] = 3
        fastpar['simN'] = 2

        # b. apply
        for key,val in fastpar.items():
            prev = getattr(par,key)
            setattr(par,key,val)
            fastpar[key] = prev

        self.allocate()

        # c. solve
        self.solve(do_assert=False)

        # d. simulate
        self.simulate()

        # e. reiterate
        for key,val in fastpar.items():
            setattr(par,key,val)
        
        self.allocate()

        toc = time.time()
        if par.do_print:
            print(f'numba precompiled in {toc-tic:.1f} secs')

    def solve_prep(self):
        """ allocate memory for solution """

        par = self.par
        sol = self.sol

        # a. standard
        keep_shape = (par.T,par.Np,par.Nn,par.Nm)        
        sol.c_keep = np.zeros(keep_shape)
        sol.inv_v_keep = np.zeros(keep_shape)
        sol.inv_marg_u_keep = np.zeros(keep_shape)

        adj_shape = (par.T,par.Np,par.Nx)
        sol.d_adj = np.zeros(adj_shape)
        sol.c_adj = np.zeros(adj_shape)
        sol.inv_v_adj = np.zeros(adj_shape)
        sol.inv_marg_u_adj = np.zeros(adj_shape)
            
        post_shape = (par.T-1,par.Np,par.Nn,par.Na)
        sol.inv_w = np.nan*np.zeros(post_shape)
        sol.q = np.nan*np.zeros(post_shape)
        sol.q_c = np.nan*np.zeros(post_shape)
        sol.q_m = np.nan*np.zeros(post_shape)


    def solve(self,do_assert=True):
        """ solve the model
        
        Args:

            do_assert (bool,optional): make assertions on the solution
        
        """

        tic = time.time()
            
        # backwards induction
        for t in reversed(range(self.par.T)):
            
            self.par.t = t

            with jit(self) as model:

                par = model.par
                sol = model.sol
                
                # i. last period
                if t == par.T-1:

                    last_period.solve(t,sol,par)

                    if do_assert:
                        assert np.all((sol.c_keep[t] >= 0) & (np.isnan(sol.c_keep[t]) == False))
                        assert np.all((sol.inv_v_keep[t] >= 0) & (np.isnan(sol.inv_v_keep[t]) == False))
                        assert np.all((sol.d_adj[t] >= 0) & (np.isnan(sol.d_adj[t]) == False))
                        assert np.all((sol.c_adj[t] >= 0) & (np.isnan(sol.c_adj[t]) == False))
                        assert np.all((sol.inv_v_adj[t] >= 0) & (np.isnan(sol.inv_v_adj[t]) == False))

                # ii. all other periods
                else:
                    
                    # o. compute post-decision functions
                    tic_w = time.time()

                    if par.solmethod in ['nvfi']:
                        post_decision.compute_wq(t,sol,par)
                    else: 
                        post_decision.compute_wq(t,sol,par,compute_q=True)                

                    toc_w = time.time()
                    par.time_w[t] = toc_w-tic_w
                    if par.do_print:
                        print(f'  w computed in {toc_w-tic_w:.1f} secs')

                    if do_assert and par.solmethod in ['nvfi','negm']:
                        assert np.all((sol.inv_w[t] > 0) & (np.isnan(sol.inv_w[t]) == False)), t 
                        if par.solmethod in ['negm']:                                                       
                            assert np.all((sol.q[t] > 0) & (np.isnan(sol.q[t]) == False)), t

                    # oo. solve keeper problem
                    tic_keep = time.time()
                    
                    if par.solmethod == 'vfi':
                        vfi.solve_keep(t,sol,par)
                    elif par.solmethod == 'nvfi':                
                        nvfi.solve_keep(t,sol,par)
                    elif par.solmethod == 'negm':
                        negm.solve_keep(t,sol,par)                                     

                    toc_keep = time.time()
                    par.time_keep[t] = toc_keep-tic_keep
                    if par.do_print:
                        print(f'  solved keeper problem in {toc_keep-tic_keep:.1f} secs')

                    if do_assert:
                        assert np.all((sol.c_keep[t] >= 0) & (np.isnan(sol.c_keep[t]) == False)), t
                        assert np.all((sol.inv_v_keep[t] >= 0) & (np.isnan(sol.inv_v_keep[t]) == False)), t

                    # ooo. solve adjuster problem
                    tic_adj = time.time()
                    
                    if par.solmethod == 'vfi':
                        vfi.solve_adj(t,sol,par)
                    elif par.solmethod in ['nvfi','negm']:
                        nvfi.solve_adj(t,sol,par)                  

                    toc_adj = time.time()
                    par.time_adj[t] = toc_adj-tic_adj
                    if par.do_print:
                        print(f'  solved adjuster problem in {toc_adj-tic_adj:.1f} secs')

                    if do_assert:
                        assert np.all((sol.d_adj[t] >= 0) & (np.isnan(sol.d_adj[t]) == False)), t
                        assert np.all((sol.c_adj[t] >= 0) & (np.isnan(sol.c_adj[t]) == False)), t
                        assert np.all((sol.inv_v_adj[t] >= 0) & (np.isnan(sol.inv_v_adj[t]) == False)), t

                # iii. print
                toc = time.time()
                if par.do_print or par.do_print_period:
                    print(f' t = {t} solved in {toc-tic:.1f} secs')

    ############
    # simulate #
    ############

    def simulate_prep(self):
        """ allocate memory for simulation """

        par = self.par
        sim = self.sim

        # a. initial and final
        sim.p0 = np.zeros(par.simN)
        sim.d0 = np.zeros(par.simN)
        sim.a0 = np.zeros(par.simN)

        sim.utility = np.zeros(par.simN)

        # b. states and choices
        sim_shape = (par.T,par.simN)
        sim.p = np.zeros(sim_shape)
        sim.y = np.zeros(sim_shape)
        sim.m = np.zeros(sim_shape)

        sim.n = np.zeros(sim_shape)
        sim.discrete = np.zeros(sim_shape,dtype=np.int)

        sim.d = np.zeros(sim_shape)
        sim.c = np.zeros(sim_shape)
        sim.a = np.zeros(sim_shape)
        
        # c. euler
        euler_shape = (par.T-1,par.simN)
        sim.euler_error = np.zeros(euler_shape)
        sim.euler_error_c = np.zeros(euler_shape)
        sim.euler_error_rel = np.zeros(euler_shape)

        # d. shocks
        sim.psi = np.zeros((par.T,par.simN))
        sim.xi = np.zeros((par.T,par.simN))
        sim.z = np.zeros(par.T)    # economy wide shock

    def simulate(self,do_utility=False,do_euler_error=False):
        """ simulate the model """

        par = self.par
        sol = self.sol
        sim = self.sim

        tic = time.time()

        # a. random shocks
        sim.p0[:] = np.random.lognormal(mean=0,sigma=par.sigma_p0,size=par.simN)
        sim.d0[:] = par.mu_d0*np.random.lognormal(mean=0,sigma=par.sigma_d0,size=par.simN)
        sim.a0[:] = par.mu_a0*np.random.lognormal(mean=0,sigma=par.sigma_a0,size=par.simN)

        I = np.random.choice(par.Nshocks,
            size=(par.T,par.simN), 
            p=par.psi_w*par.xi_w*par.z_w)
        sim.psi[:,:] = par.psi[I]
        sim.xi[:,:] = par.xi[I]
        sim.z[:] = par.z[I[:,0]]

        # b. call
        with jit(self) as model:

            par = model.par
            sol = model.sol
            sim = model.sim

            simulate.lifecycle(sim,sol,par)

        toc = time.time()
        
        if par.do_print:
            print(f'model simulated in {toc-tic:.1f} secs')

        # d. euler errors
        def norm_euler_errors(model):
            return np.log10(abs(model.sim.euler_error/model.sim.euler_error_c)+1e-8)

        tic = time.time()        
        if do_euler_error:

            with jit(self) as model:

                par = model.par
                sol = model.sol
                sim = model.sim

                simulate.euler_errors(sim,sol,par)
            
            sim.euler_error_rel[:] = norm_euler_errors(self)
        
        toc = time.time()
        if par.do_print:
            print(f'euler errors calculated in {toc-tic:.1f} secs')

        # e. utility
        tic = time.time()        
        if do_utility:
            simulate.calc_utility(sim,sol,par)
        
        toc = time.time()
        if par.do_print:
            print(f'utility calculated in {toc-tic:.1f} secs')

    ########
    # figs #
    ########

    def decision_functions(self):
        figs.decision_functions(self)

    def egm(self):        
        figs.egm(self)

    def lifecycle(self,deciles=False):        
        figs.lifecycle(self,deciles=deciles)

    ###########
    # analyze #
    ###########

    # def analyze(self,solve=True,do_assert=True,**kwargs):
        
    #     par = self.par

    #     for key,val in kwargs.items():
    #         setattr(par,key,val)
        
    #     self.create_grids()

    #     # solve and simulate
    #     if solve:
    #         self.precompile_numba()
    #         self.solve(do_assert)
    #     self.simulate(do_euler_error=True,do_utility=True)

    #     # print
    #     self.print_analysis()

    # def print_analysis(self):

    #     par = self.par
    #     sim = self.sim

    #     def avg_euler_error(model,I):
    #         if I.any():
    #             return np.nanmean(model.sim.euler_error_rel.ravel()[I])
    #         else:
    #             return np.nan

    #     def percentile_euler_error(model,I,p):
    #         if I.any():
    #             return np.nanpercentile(model.sim.euler_error_rel.ravel()[I],p)
    #         else:
    #             return np.nan

    #     # population
    #     keepers = sim.discrete[:-1,:].ravel() == 0
    #     adjusters = sim.discrete[:-1,:].ravel() > 0
    #     everybody = keepers | adjusters

    #     # print
    #     time = par.time_w+par.time_adj+par.time_keep
    #     txt = f'Name: {self.name} (solmethod = {par.solmethod})\n'
    #     txt += f'Grids: (p,n,m,x,a) = ({par.Np},{par.Nn},{par.Nm},{par.Nx},{par.Na})\n'
        
    #     txt += 'Timings:\n'
    #     txt += f' total: {np.sum(time):.1f}\n'
    #     txt += f'     w: {np.sum(par.time_w):.1f}\n'
    #     txt += f'  keep: {np.sum(par.time_keep):.1f}\n'
    #     txt += f'   adj: {np.sum(par.time_adj):.1f}\n'
    #     txt += f'Utility: {np.mean(sim.utility):.6f}\n'
        
    #     txt += 'Euler errors:\n'
    #     txt += f'     total: {avg_euler_error(self,everybody):.2f} ({percentile_euler_error(self,everybody,5):.2f},{percentile_euler_error(self,everybody,95):.2f})\n'
    #     txt += f'   keepers: {avg_euler_error(self,keepers):.2f} ({percentile_euler_error(self,keepers,5):.2f},{percentile_euler_error(self,keepers,95):.2f})\n'
    #     txt += f' adjusters: {avg_euler_error(self,adjusters):.2f} ({percentile_euler_error(self,adjusters,5):.2f},{percentile_euler_error(self,adjusters,95):.2f})\n'

    #     txt += 'Moments:\n'

    #     txt += f' adjuster share: {np.mean(sim.discrete):.3f}\n'
        
    #     txt += f'         mean c: {np.mean(sim.c):.3f}\n'
    #     txt += f'          var c: {np.var(sim.c):.3f}\n'


    #     txt += f'         mean d: {np.mean(sim.d):.3f}\n'
    #     txt += f'          var d: {np.var(sim.d):.3f}\n'

    #     print(txt)