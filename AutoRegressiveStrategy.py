import pandas as pd
import numpy as np
import math
import arch
import UNI_v3_funcs
import logging
import ActiveStrategyFramework
import scipy
# logging.basicConfig(filename='ar_strategy.log',level=logging.INFO)


class AlphaParameterException(Exception):
    pass

class TauParameterException(Exception):
    pass

class BoundsInvertedError(Exception):
    pass

class AutoRegressiveStrategy:
    def __init__(self,model_data,alpha_param,tau_param,volatility_reset_ratio,tokens_outside_reset = .05,data_frequency='D'):
        
        self.model_data             = self.clean_data_for_garch(model_data)
        self.alpha_param            = alpha_param
        self.tau_param              = tau_param
        self.volatility_reset_ratio = volatility_reset_ratio
        self.data_frequency         = data_frequency
        self.tokens_outside_reset   = tokens_outside_reset
        
        # Allow for different input data frequencies, always get 1 day ahead forecast
        # Model data frequency is expressed in minutes
        
        if   data_frequency == 'D':
            self.annualization_factor = 365**.5
            self.resample_option      = '1D'
        elif data_frequency == 'H':
            self.annualization_factor = (24*365)**.5
            self.resample_option      = '1H'
        elif data_frequency == 'M':
            self.annualization_factor = (60*24*365)**.5
            self.resample_option      = '1 min'
        
    #####################################
    # Estimate AR model at current timepoint
    #####################################
    
    def clean_data_for_garch(self,data_in):
            z_score_cutoff               = 3
            data_filled                  = ActiveStrategyFramework.fill_time(data_in)
            data_filled['z_scores']      = np.abs(scipy.stats.zscore(data_filled['quotePrice']))
            data_filled                  = data_filled.drop(data_filled[data_filled.z_scores > z_score_cutoff].index)
            data_filled['price_return']  = data_filled['quotePrice'].pct_change()
            data_filled['z_scores']      = np.abs(scipy.stats.zscore(data_filled['quotePrice']))
            data_filled                  = data_filled.drop(data_filled[data_filled.z_scores > z_score_cutoff].index)
            return data_filled
        
        
    def generate_model_forecast(self,timepoint):
        
            # Compute returns with data_frequency frequency starting at the current timepoint and looking backwards
            current_data                  = self.model_data.loc[:timepoint].resample(self.resample_option,closed='right',label='right',origin=timepoint).last()            
            current_data['price_return']  = current_data['quotePrice'].pct_change()
            current_data                  = current_data.dropna(axis=0)
            
            ar_model             = arch.univariate.ARX(current_data.price_return[(current_data.index >= (timepoint - pd.Timedelta('90 days')))].to_numpy(), lags=1,rescale=True)
            ar_model.volatility  = arch.univariate.GARCH(p=1,q=1)
            
            res                  = ar_model.fit(update_freq=0, disp="off")
            scale                = res.scale

            forecasts            = res.forecast(horizon=1, reindex=False)
            
            var_forecast         = forecasts.variance.to_numpy()[0][-1]

            return_forecast      = forecasts.mean.to_numpy()[0][-1] / scale
            
            sd_forecast          = self.annualization_factor*(var_forecast/np.power(scale,2))**(0.5)

            result_dict          = {'return_forecast': return_forecast,
                                    'sd_forecast'    : sd_forecast}
            
            return result_dict
        
    #####################################
    # Check if a rebalance is necessary. 
    # If it is, remove the liquidity and set new ranges
    #####################################
        
    def check_strategy(self,current_strat_obs,strategy_info):
        
        #####################################
        #
        # This strategy rebalances in three scenarios:
        # 1. Leave Reset Range
        # 2. Volatility has dropped           (volatility_reset_ratio)
        # 3. Tokens outside of pool greater than 5% of value of LP position
        #
        #####################################
        
        model_forecast      = None
        LIMIT_ORDER_BALANCE = current_strat_obs.liquidity_ranges[1]['token_0'] + current_strat_obs.liquidity_ranges[1]['token_1']*current_strat_obs.price
        BASE_ORDER_BALANCE  = current_strat_obs.liquidity_ranges[0]['token_0'] + current_strat_obs.liquidity_ranges[0]['token_1']*current_strat_obs.price
        
        #######################
        # 1. Leave Reset Range
        #######################
        LEFT_RANGE_LOW      = current_strat_obs.price < strategy_info['reset_range_lower']
        LEFT_RANGE_HIGH     = current_strat_obs.price > strategy_info['reset_range_upper']

        #######################
        # 2. Volatility has dropped 
        #######################
        # Rebalance if volatility has gone down significantly
        # When volatility increases the reset range will be hit
        # Check every hour (60  minutes)
        
        ar_check_frequency = 60        
        time_since_reset =  current_strat_obs.time - current_strat_obs.liquidity_ranges[0]['reset_time']
        
        VOL_REBALANCE    = False
        if divmod(time_since_reset.total_seconds(), 60)[0] % ar_check_frequency == 0:

            model_forecast = self.generate_model_forecast(current_strat_obs.time)
        
            if model_forecast['sd_forecast']/current_strat_obs.liquidity_ranges[0]['volatility'] <= self.volatility_reset_ratio:
                VOL_REBALANCE = True
            else:
                VOL_REBALANCE = False
        
        #######################
        # 3. Tokens outside of pool greater than 5% of value of LP position
        #######################
        
        left_over_balance = current_strat_obs.token_0_left_over + current_strat_obs.token_1_left_over*current_strat_obs.price                
        
        if (left_over_balance > self.tokens_outside_reset * (LIMIT_ORDER_BALANCE + BASE_ORDER_BALANCE)):
            TOKENS_OUTSIDE_LARGE = True
        else:
            TOKENS_OUTSIDE_LARGE = False
        

        # if a reset is necessary
        if (((LEFT_RANGE_LOW | LEFT_RANGE_HIGH) | VOL_REBALANCE) | TOKENS_OUTSIDE_LARGE):
            current_strat_obs.reset_point = True
            
            if (LEFT_RANGE_LOW | LEFT_RANGE_HIGH):
                current_strat_obs.reset_reason = 'exited_range'
            elif VOL_REBALANCE:
                current_strat_obs.reset_reason = 'vol_rebalance'
            elif TOKENS_OUTSIDE_LARGE:
                current_strat_obs.reset_reason = 'tokens_outside_large'
            
            # Remove liquidity and claim fees 
            current_strat_obs.remove_liquidity()
            
            # Reset liquidity            
            liq_range,strategy_info = self.set_liquidity_ranges(current_strat_obs,model_forecast)
            return liq_range,strategy_info        
        else:
            return current_strat_obs.liquidity_ranges,strategy_info
            
            
    def set_liquidity_ranges(self,current_strat_obs,model_forecast = None):
        
        ###########################################################
        # STEP 1: Do calculations required to determine base liquidity bounds
        ###########################################################
        
        # Fit model
        if model_forecast is None:
            model_forecast = self.generate_model_forecast(current_strat_obs.time)
            
        # Limit return prediction to a 25% change
        if np.abs(model_forecast['return_forecast']) > .25:
                    model_forecast['return_forecast'] = np.sign(model_forecast['return_forecast'])*.25
                
        target_price     = (1 + model_forecast['return_forecast']) * current_strat_obs.price
        
        # Check paramters won't lead to prices outisde the relevant range:
        if self.alpha_param*model_forecast['sd_forecast'] > (1 + model_forecast['return_forecast']):
            raise AlphaParameterException('Alpha parameter {:.3f} too large for measured volatility, will lead to negative prices: sd {:.3f} band {:.3f} forecast {:.3f} current {:.6f} time {}'. \
                                          format(self.alpha_param,model_forecast['sd_forecast'],self.alpha_param*model_forecast['sd_forecast'],model_forecast['return_forecast'],current_strat_obs.price,
                                                 current_strat_obs.time))
        elif self.tau_param*model_forecast['sd_forecast'] > (1 + model_forecast['return_forecast']):
            raise TauParameterException('Tau parameter {:.3f} too large for measured volatility, will lead to negative prices: sd {:.3f} band {:.3f} forecast {:.3f} current {:.6f} time {}'. \
                                          format(self.tau_param,model_forecast['sd_forecast'],self.tau_param*model_forecast['sd_forecast'],model_forecast['return_forecast'],current_strat_obs.price,
                                                 current_strat_obs.time))

        # Set the base range
        base_range_lower           = current_strat_obs.price * (1 + model_forecast['return_forecast'] - self.alpha_param*model_forecast['sd_forecast'])
        base_range_upper           = current_strat_obs.price * (1 + model_forecast['return_forecast'] + self.alpha_param*model_forecast['sd_forecast'])
        
        # Set the reset range
        strategy_info = dict()
        strategy_info['reset_range_lower'] = current_strat_obs.price * (1 + model_forecast['return_forecast'] - self.tau_param*model_forecast['sd_forecast'])
        strategy_info['reset_range_upper'] = current_strat_obs.price * (1 + model_forecast['return_forecast'] + self.tau_param*model_forecast['sd_forecast'])
        
        save_ranges                = []
        
        ########################################################### 
        # STEP 2: Set Base Liquidity
        ###########################################################
        
        # Store each token amount supplied to pool
        total_token_0_amount = current_strat_obs.liquidity_in_0
        total_token_1_amount = current_strat_obs.liquidity_in_1
        
#         logging.info('BASE POSITION: lower {} | upper {} | current {} | target {} \n return forecast {} | sd forecast {} | reset reason {} | time {}'.format(base_range_lower,
#                                                                                                               base_range_upper,current_strat_obs.price,
#                                                                                                               target_price,
#                                                                                                               model_forecast['return_forecast'],
#                                                                                                               model_forecast['sd_forecast'],
#                                                                                                               current_strat_obs.reset_reason,
#                                                                                                               current_strat_obs.time))
                                    
        # Lower Range
        TICK_A_PRE         = int(math.log(current_strat_obs.decimal_adjustment*base_range_lower,1.0001))
        TICK_A             = int(round(TICK_A_PRE/current_strat_obs.tickSpacing)*current_strat_obs.tickSpacing)

        # Upper Range
        TICK_B_PRE        = int(math.log(current_strat_obs.decimal_adjustment*base_range_upper,1.0001))
        TICK_B            = int(round(TICK_B_PRE/current_strat_obs.tickSpacing)*current_strat_obs.tickSpacing)
        
        # Make sure Tick A < Tick B. If not make one tick
        if TICK_A == TICK_B:
            TICK_B = TICK_A + current_strat_obs.tickSpacing
        elif TICK_A > TICK_B:
            raise BoundsInvertedError

        
        liquidity_placed_base   = int(UNI_v3_funcs.get_liquidity(current_strat_obs.price_tick,TICK_A,TICK_B,current_strat_obs.liquidity_in_0, \
                                                                       current_strat_obs.liquidity_in_1,current_strat_obs.decimals_0,current_strat_obs.decimals_1))
        
        base_0_amount,base_1_amount   = UNI_v3_funcs.get_amounts(current_strat_obs.price_tick,TICK_A,TICK_B,liquidity_placed_base\
                                                                 ,current_strat_obs.decimals_0,current_strat_obs.decimals_1)
        
#         logging.info('base 0: {} | base 1: {}'.format(base_0_amount,base_1_amount))
        
        total_token_0_amount  -= base_0_amount
        total_token_1_amount  -= base_1_amount

        base_liq_range =       {'price'              : current_strat_obs.price,
                                'target_price'       : target_price,
                                'lower_bin_tick'     : TICK_A,
                                'upper_bin_tick'     : TICK_B,
                                'lower_bin_price'    : base_range_lower,
                                'upper_bin_price'    : base_range_upper,
                                'time'               : current_strat_obs.time,
                                'token_0'            : base_0_amount,
                                'token_1'            : base_1_amount,
                                'position_liquidity' : liquidity_placed_base,
                                'volatility'         : model_forecast['sd_forecast'],
                                'reset_time'         : current_strat_obs.time,
                                'return_forecast'    : model_forecast['return_forecast']}

        save_ranges.append(base_liq_range)

        ###########################
        # Step 3: Set Limit Position 
        ############################
        
        limit_amount_0 = total_token_0_amount
        limit_amount_1 = total_token_1_amount
        
        # Place singe sided highest value
        if limit_amount_0*current_strat_obs.price > limit_amount_1:        
            # Place Token 0
            limit_amount_1 = 0.0
            limit_range_lower = current_strat_obs.price
            limit_range_upper = base_range_upper                     
        else:
            # Place Token 1
            limit_amount_0 = 0.0
            limit_range_lower = base_range_lower
            limit_range_upper = current_strat_obs.price
            
            
        TICK_A_PRE         = int(math.log(current_strat_obs.decimal_adjustment*limit_range_lower,1.0001))
        TICK_A             = int(round(TICK_A_PRE/current_strat_obs.tickSpacing)*current_strat_obs.tickSpacing)

        TICK_B_PRE        = int(math.log(current_strat_obs.decimal_adjustment*limit_range_upper,1.0001))
        TICK_B            = int(round(TICK_B_PRE/current_strat_obs.tickSpacing)*current_strat_obs.tickSpacing)
        
#         logging.info('LIMIT POSITION: lower {} | upper {}'.format(limit_range_lower,limit_range_upper))
        
        # Make sure Tick A < Tick B. If not make one tick
        # Relevant mostly for stablecoin pairs
        if TICK_A == TICK_B:
            TICK_B = TICK_A + current_strat_obs.tickSpacing
        elif TICK_A > TICK_B:
            if limit_amount_0*current_strat_obs.price > limit_amount_1:
                TICK_B = TICK_A + current_strat_obs.tickSpacing
            else: 
                TICK_A = TICK_B - current_strat_obs.tickSpacing

        liquidity_placed_limit        = int(UNI_v3_funcs.get_liquidity(current_strat_obs.price_tick,TICK_A,TICK_B, \
                                                                       limit_amount_0,limit_amount_1,current_strat_obs.decimals_0,current_strat_obs.decimals_1))
        limit_0_amount,limit_1_amount =     UNI_v3_funcs.get_amounts(current_strat_obs.price_tick,TICK_A,TICK_B,\
                                                                     liquidity_placed_limit,current_strat_obs.decimals_0,current_strat_obs.decimals_1)  
        
                     
#         logging.info('limit 0: {} | limit 1: {}'.format(limit_0_amount,limit_1_amount))

        limit_liq_range =       {'price'              : current_strat_obs.price,
                                 'target_price'       : target_price,
                                 'lower_bin_tick'     : TICK_A,
                                 'upper_bin_tick'     : TICK_B,
                                 'lower_bin_price'    : limit_range_lower,
                                 'upper_bin_price'    : limit_range_upper,                                 
                                 'time'               : current_strat_obs.time,
                                 'token_0'            : limit_0_amount,
                                 'token_1'            : limit_1_amount,
                                 'position_liquidity' : liquidity_placed_limit,
                                 'volatility'         : model_forecast['sd_forecast'],
                                 'reset_time'         : current_strat_obs.time,
                                 'return_forecast'    : model_forecast['return_forecast']}     

        save_ranges.append(limit_liq_range)
        

        # Update token amount supplied to pool
        total_token_0_amount  -= limit_0_amount
        total_token_1_amount  -= limit_1_amount
        
        # Check we didn't allocate more liquidiqity than available
        assert current_strat_obs.liquidity_in_0 >= total_token_0_amount
        assert current_strat_obs.liquidity_in_1 >= total_token_1_amount
        
        # How much liquidity is not allcated to ranges
        current_strat_obs.token_0_left_over = max([total_token_0_amount,0.0])
        current_strat_obs.token_1_left_over = max([total_token_1_amount,0.0])

        # Since liquidity was allocated, set to 0
        current_strat_obs.liquidity_in_0 = 0.0
        current_strat_obs.liquidity_in_1 = 0.0
        
        return save_ranges,strategy_info
        
        
    ########################################################
    # Extract strategy parameters
    ########################################################
    def dict_components(self,strategy_observation):
            this_data = dict()
            
            # General variables
            this_data['time']                   = strategy_observation.time
            this_data['price']                  = strategy_observation.price
            this_data['reset_point']            = strategy_observation.reset_point
            this_data['reset_reason']           = strategy_observation.reset_reason
            this_data['volatility']             = strategy_observation.liquidity_ranges[0]['volatility']
            this_data['return_forecast']        = strategy_observation.liquidity_ranges[0]['return_forecast']
            
            
            # Range Variables
            this_data['base_range_lower']       = strategy_observation.liquidity_ranges[0]['lower_bin_price']
            this_data['base_range_upper']       = strategy_observation.liquidity_ranges[0]['upper_bin_price']
            this_data['limit_range_lower']      = strategy_observation.liquidity_ranges[1]['lower_bin_price']
            this_data['limit_range_upper']      = strategy_observation.liquidity_ranges[1]['upper_bin_price']
            this_data['reset_range_lower']      = strategy_observation.strategy_info['reset_range_lower']
            this_data['reset_range_upper']      = strategy_observation.strategy_info['reset_range_upper']
            
            # Fee Varaibles
            this_data['token_0_fees']           = strategy_observation.token_0_fees 
            this_data['token_1_fees']           = strategy_observation.token_1_fees 
            this_data['token_0_fees_uncollected']     = strategy_observation.token_0_fees_uncollected
            this_data['token_1_fees_uncollected']     = strategy_observation.token_1_fees_uncollected
            
            # Asset Variables
            this_data['token_0_left_over']      = strategy_observation.token_0_left_over
            this_data['token_1_left_over']      = strategy_observation.token_1_left_over
            
            total_token_0 = 0.0
            total_token_1 = 0.0
            for i in range(len(strategy_observation.liquidity_ranges)):
                total_token_0 += strategy_observation.liquidity_ranges[i]['token_0']
                total_token_1 += strategy_observation.liquidity_ranges[i]['token_1']
                
            this_data['token_0_allocated']      = total_token_0
            this_data['token_1_allocated']      = total_token_1
            this_data['token_0_total']          = total_token_0 + strategy_observation.token_0_left_over + strategy_observation.token_0_fees_uncollected
            this_data['token_1_total']          = total_token_1 + strategy_observation.token_1_left_over + strategy_observation.token_1_fees_uncollected

            # Value Variables
            this_data['value_position']         = this_data['token_0_total']     + this_data['token_1_total']     / this_data['price']
            this_data['value_allocated']        = this_data['token_0_allocated'] + this_data['token_1_allocated'] / this_data['price']
            this_data['value_left_over']        = this_data['token_0_left_over'] + this_data['token_1_left_over'] / this_data['price']
            
            this_data['base_position_value']    = strategy_observation.liquidity_ranges[0]['token_0'] + strategy_observation.liquidity_ranges[0]['token_1'] * this_data['price']
            this_data['limit_position_value']   = strategy_observation.liquidity_ranges[1]['token_0'] + strategy_observation.liquidity_ranges[1]['token_1'] * this_data['price']
             
            return this_data